"""OMDb ratings pass: attach Rotten Tomatoes / Metacritic / IMDb scores and
the awards note to TMDB-matched films.

Free-tier friendly: every lookup (hit or confirmed miss) is cached by tmdb_id,
transient failures are NOT cached so they retry next run, and new lookups are
capped per run. Route: TMDB external_ids (tmdb_id -> imdb_id), then OMDb by
imdb_id — one OMDb call per film, ever.
"""
import json
import pathlib
import ssl
import sys
import urllib.error
import urllib.request

from . import config

UA = "Marquee/0.1 (personal NYC moviegoing tool)"
MAX_NEW_PER_RUN = 400


def _ssl_context():
    # Same rationale as the adapters: python.org macOS builds ship an empty
    # CA store; load the system bundle. Verification stays on.
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except Exception:
            pass
    return ctx


_CTX = None


def _get(url):
    global _CTX
    if _CTX is None:
        _CTX = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def _lookup(tmdb_id, tmdb_key, omdb_key):
    """Returns a ratings dict, {} for a confirmed miss, or None on transient failure."""
    try:
        ext = _get(f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_key}")
        imdb_id = ext.get("imdb_id")
        if not imdb_id:
            return {}
        o = _get(f"https://www.omdbapi.com/?i={imdb_id}&apikey={omdb_key}")
        if o.get("Response") != "True":
            return {"imdb_id": imdb_id}

        def clean(v):
            return None if v in (None, "", "N/A") else v

        rt = next((r.get("Value") for r in o.get("Ratings", [])
                   if r.get("Source") == "Rotten Tomatoes"), None)
        return {
            "imdb_id": imdb_id,
            "rt": clean(rt),
            "metascore": clean(o.get("Metascore")),
            "imdb_rating": clean(o.get("imdbRating")),
            "awards": clean(o.get("Awards")),
        }
    except Exception as e:
        print(f"[marquee] omdb: lookup failed for tmdb {tmdb_id}: {e}", file=sys.stderr)
        return None


def attach_ratings(films: dict, cache_path) -> dict:
    """Mutates and returns the films map, adding rt/metascore/imdb_rating/awards."""
    omdb_key = config.get("OMDB_API_KEY")
    tmdb_key = config.get("TMDB_API_KEY")
    if not omdb_key or not tmdb_key:
        print("[marquee] omdb: key missing; skipping ratings", file=sys.stderr)
        return films

    cache_path = pathlib.Path(cache_path)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    new = deferred = 0

    for f in films.values():
        tid = str(f.get("tmdb_id") or "")
        if not tid:
            continue
        if tid in cache:
            entry = cache[tid]
        else:
            if new >= MAX_NEW_PER_RUN:
                deferred += 1
                continue
            new += 1
            entry = _lookup(tid, tmdb_key, omdb_key)
            if entry is not None:          # transient failures retry next run
                cache[tid] = entry
        if entry:
            for k in ("imdb_id", "rt", "metascore", "imdb_rating", "awards"):
                if entry.get(k):
                    f[k] = entry[k]

    cache_path.write_text(json.dumps(cache, separators=(",", ":")))
    rated = sum(1 for f in films.values() if f.get("rt") or f.get("metascore"))
    msg = f"[marquee] omdb: ratings on {rated}/{len(films)} films ({new} new lookups)"
    if deferred:
        msg += f"; {deferred} deferred to next run"
    print(msg, file=sys.stderr)
    return films


if __name__ == "__main__":
    data = json.loads(pathlib.Path("build/site/data.json").read_text())
    films = attach_ratings(data.get("films", {}), pathlib.Path("data/omdb_cache.json"))
    shown = 0
    for f in films.values():
        if f.get("rt") or f.get("metascore"):
            print(f"  {f['title']} ({f.get('year')}): RT {f.get('rt')} · MC {f.get('metascore')} · awards: {str(f.get('awards'))[:60]}")
            shown += 1
            if shown >= 5:
                break

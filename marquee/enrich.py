"""TMDB enrichment: match canonical screenings to TMDB films.

enrich(screenings, cache_path) -> {film_key: {tmdb_id, title, year, poster,
trailer, genres}} — entries only for confidently-matched films.

Matching (probed live 2026-08-17):
    /search/movie?query=...&primary_release_year=YYYY  (year only when known)
      -> accept when a result's title/original_title equals the query after
         normalization (casefold, accents and punctuation stripped)
    /movie/{id}/alternative_titles  -> only to confirm otherwise-ambiguous hits
    /movie/{id}/credits             -> director tiebreak, only for multi-hit
                                       cases where the screening has a director
    /movie/{id}/videos              -> trailer (prefer official YouTube Trailer)
    /genre/movie/list               -> genre-id -> name map, once per run

Rep listings sometimes carry wrong years, so a year-constrained search that
misses is retried without the year. Multi-film program titles ("X + Y",
lowercase series-ish blurbs) are skipped rather than mismatched.

Cache: JSON at cache_path storing every lookup INCLUDING misses
({"film_key|year": result-or-null}), loaded at start, saved at end, so repeat
runs cost near-zero API calls. New lookups are capped per run (MAX_NEW_LOOKUPS);
the overflow catches up on the next run.

No TMDB_API_KEY -> stderr warning and {} (never crashes the pipeline).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import ssl
import sys
import time as _time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .normalize import _title_key

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT_S = 20
MAX_RETRIES = 2            # retries beyond the first attempt
BACKOFF_BASE_S = 2.0       # sleep 2s, then 4s
MIN_REQUEST_GAP_S = 0.05   # TMDB allows ~50 req/s; stay serial and modest
MAX_NEW_LOOKUPS = 250      # new (uncached) film lookups per run
MAX_CONSECUTIVE_FAILURES = 5  # stop burning the budget if TMDB is down/401ing


# --- HTTP plumbing -----------------------------------------------------------

_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """A verifying SSL context that works even on python.org macOS builds.

    Those builds ship no CA bundle until "Install Certificates.command" is run,
    so a default context has zero CAs and every HTTPS request fails. Detect
    that and fall back to certifi (if importable) or a known system bundle.
    Verification is never disabled.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        candidates: list[str] = []
        try:
            import certifi  # not required, used only if already installed
            candidates.append(certifi.where())
        except ImportError:
            pass
        candidates += [
            "/etc/ssl/cert.pem",                    # macOS system bundle
            "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu
            "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/Fedora
        ]
        for cafile in candidates:
            if os.path.isfile(cafile):
                ctx = ssl.create_default_context(cafile=cafile)
                break
    _SSL_CONTEXT = ctx
    return ctx


_last_request_at = 0.0


class _Tmdb:
    """Serial TMDB client. Supports v3 keys (api_key param) and v4 tokens (Bearer)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.is_v4 = api_key.startswith("eyJ")  # v4 read token is a JWT
        self.requests_made = 0

    def get(self, path: str, params: dict | None = None):
        """GET a v3 API path, return parsed JSON. Retries with backoff."""
        global _last_request_at
        params = dict(params or {})
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.is_v4:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_err: Exception | None = None
        for attempt in range(1 + MAX_RETRIES):
            if attempt:
                _time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
            gap = MIN_REQUEST_GAP_S - (_time.monotonic() - _last_request_at)
            if gap > 0:
                _time.sleep(gap)
            _last_request_at = _time.monotonic()
            self.requests_made += 1
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=TIMEOUT_S,
                                            context=_ssl_context()) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                if err.code == 429:  # rate-limited: honor Retry-After, then retry
                    retry_after = err.headers.get("Retry-After")
                    try:
                        _time.sleep(min(float(retry_after), 30.0))
                    except (TypeError, ValueError):
                        pass
                    last_err = err
                    continue
                if err.code in (401, 403):  # bad key: retrying is pointless
                    raise RuntimeError(f"TMDB auth failed (HTTP {err.code})") from err
                last_err = err
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
                last_err = err
        raise RuntimeError(
            f"TMDB request failed after {1 + MAX_RETRIES} attempts: {path}") from last_err


# --- Title handling ----------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm_title(s: str) -> str:
    """Comparison form: accents stripped, casefolded, punctuation -> space."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", s.casefold()).strip()


# Rep listings decorate titles; peel trailing tags so the TMDB query is clean.
_TRAILING_YEAR = re.compile(r"\s*[(\[](\d{4})[)\]]\s*$")
_TRAILING_DECOR = [
    re.compile(r"(?:\s*[:\-–—]\s*|\s+in\s+|\s+on\s+)"
               r"(?:35\s?mm|70\s?mm|16\s?mm|8\s?mm|4k(?:\s+restoration)?|imax|"
               r"digital\s+restoration|dcp)\s*!?\s*$", re.I),
    re.compile(r"\s*[:\-–—]\s*(?:the\s+)?director'?s\s+cut\s*$", re.I),
]


def _query_title(title: str, year: int | None) -> tuple[str, int | None]:
    """Strip listing decorations; harvest a trailing '(YYYY)' as year if needed."""
    t = (title or "").strip()
    m = _TRAILING_YEAR.search(t)
    if m:
        t = t[: m.start()].strip()
        if year is None:
            y = int(m.group(1))
            if 1890 <= y <= 2100:
                year = y
    changed = True
    while changed:
        changed = False
        for rx in _TRAILING_DECOR:
            m = rx.search(t)
            if m and m.start() > 0:  # never strip a title down to nothing
                t = t[: m.start()].strip()
                changed = True
    return (t or title.strip(), year)


def _is_program(title: str) -> bool:
    """Multi-film programs / series blurbs we must not force-match to one film."""
    t = (title or "").strip()
    if " + " in t:
        return True
    # Lowercase-start multiword blurbs ("an evening of...", "shorts from...").
    # Single lowercase words are real stylized titles (eXistenZ), keep those.
    first_alpha = next((ch for ch in t if ch.isalpha()), "")
    return bool(first_alpha and first_alpha.islower() and " " in t)


def _director_names(s: str | None) -> list[str]:
    if not s:
        return []
    parts = re.split(r"\s*(?:,|&| and )\s*", s)
    return [_norm_title(p) for p in parts if p.strip()]


def _directors_match(src: str | None, tmdb_directors: list[str]) -> bool:
    src_names = _director_names(src)
    tmdb_names = [_norm_title(d) for d in tmdb_directors]
    for a in src_names:
        for b in tmdb_names:
            if not a or not b:
                continue
            if a == b or a in b or b in a:
                return True
            if a.split()[-1] == b.split()[-1]:  # surname-only listings
                return True
    return False


# --- Matching ----------------------------------------------------------------

def _result_year(result: dict) -> int | None:
    rd = result.get("release_date") or ""
    if len(rd) >= 4 and rd[:4].isdigit():
        return int(rd[:4])
    return None


def _title_candidates(results: list, query_norm: str) -> list[dict]:
    out = []
    for r in results[:8]:
        if not isinstance(r, dict):
            continue
        if (_norm_title(r.get("title") or "") == query_norm
                or _norm_title(r.get("original_title") or "") == query_norm):
            out.append(r)
    return out


def _alt_title_confirm(client: _Tmdb, results: list, query_norm: str) -> dict | None:
    """Check /alternative_titles for the top hits only (ambiguity resolver)."""
    for r in results[:2]:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        data = client.get(f"/movie/{r['id']}/alternative_titles")
        for alt in data.get("titles") or []:
            if isinstance(alt, dict) and _norm_title(alt.get("title") or "") == query_norm:
                return r
    return None


def _pick_by_director(client: _Tmdb, candidates: list[dict], director: str) -> dict | None:
    matched = []
    for r in candidates[:3]:
        credits = client.get(f"/movie/{r['id']}/credits")
        directors = [c.get("name") or "" for c in credits.get("crew") or []
                     if isinstance(c, dict) and c.get("job") == "Director"]
        if _directors_match(director, directors):
            matched.append(r)
    return matched[0] if len(matched) >= 1 else None


def _match_film(client: _Tmdb, title: str, year: int | None,
                director: str | None) -> dict | None:
    """Return the accepted /search/movie result dict, or None."""
    query, year = _query_title(title, year)
    query_norm = _norm_title(query)
    if not query_norm:
        return None

    attempts: list[int | None] = [year, None] if year is not None else [None]
    for release_year in attempts:
        params = {"query": query, "include_adult": "false", "language": "en-US"}
        if release_year is not None:
            params["primary_release_year"] = release_year
        results = (client.get("/search/movie", params).get("results") or [])
        if not results:
            continue  # year-constrained miss -> retry without year
        candidates = _title_candidates(results, query_norm)
        if not candidates:
            # Foreign/retitled films surface under alternative titles.
            confirmed = _alt_title_confirm(client, results, query_norm)
            if confirmed is not None:
                return confirmed
            continue
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous multi-hit: director tiebreak when the listing names one.
        if director:
            picked = _pick_by_director(client, candidates, director)
            if picked is not None:
                return picked
        return candidates[0]  # top search rank (year already constrained it)
    return None


def _fetch_trailer(client: _Tmdb, tmdb_id: int) -> str | None:
    data = client.get(f"/movie/{tmdb_id}/videos")
    vids = [v for v in data.get("results") or []
            if isinstance(v, dict) and v.get("site") == "YouTube" and v.get("key")]
    if not vids:
        return None

    def rank(v: dict) -> tuple:
        return (v.get("type") == "Trailer", bool(v.get("official")),
                v.get("size") or 0)

    best = max(vids, key=rank)
    if best.get("type") not in ("Trailer", "Teaser"):
        return None  # only clips/featurettes exist; not a trailer
    return f"https://www.youtube.com/watch?v={best['key']}"


_GENRE_MAP: dict[int, str] | None = None


def _genre_map(client: _Tmdb) -> dict[int, str]:
    global _GENRE_MAP
    if _GENRE_MAP is None:
        try:
            data = client.get("/genre/movie/list", {"language": "en-US"})
            _GENRE_MAP = {g["id"]: g["name"] for g in data.get("genres") or []
                          if isinstance(g, dict) and "id" in g and "name" in g}
        except Exception as err:
            print(f"enrich: genre list unavailable ({err})", file=sys.stderr)
            _GENRE_MAP = {}
    return _GENRE_MAP


def _lookup(client: _Tmdb, title: str, year: int | None,
            director: str | None) -> dict | None:
    """Full lookup for one film. Returns the cacheable entry, or None (= miss)."""
    hit = _match_film(client, title, year, director)
    if hit is None or not hit.get("id"):
        return None
    tmdb_id = int(hit["id"])
    poster_path = hit.get("poster_path")
    genres = [name for gid in hit.get("genre_ids") or []
              if (name := _genre_map(client).get(gid))]
    try:
        trailer = _fetch_trailer(client, tmdb_id)
    except Exception as err:  # a matched film without a trailer is still a match
        print(f"enrich: videos failed for tmdb {tmdb_id}: {err}", file=sys.stderr)
        trailer = None
    return {
        "tmdb_id": tmdb_id,
        "title": (hit.get("title") or title).strip(),
        "year": _result_year(hit),
        "poster": f"{IMAGE_BASE}{poster_path}" if poster_path else None,
        "trailer": trailer,
        "genres": genres,
    }


# --- Cache -------------------------------------------------------------------

def _cache_key(film_key: str, year: int | None) -> str:
    return f"{film_key}|{year if year is not None else ''}"


def _load_cache(cache_path: pathlib.Path) -> dict:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as err:
        print(f"enrich: unreadable cache {cache_path} ({err}); starting fresh",
              file=sys.stderr)
        return {}


def _save_cache(cache_path: pathlib.Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True,
                                  indent=1), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as err:
        print(f"enrich: could not save cache {cache_path}: {err}", file=sys.stderr)


# --- Public entry point ------------------------------------------------------

def enrich(screenings: list[dict], cache_path) -> dict:
    """Match each distinct film_key in `screenings` against TMDB.

    Returns {film_key: {tmdb_id, title, year, poster, trailer, genres}} with
    entries only for confidently-matched films. Every lookup (hit or miss) is
    cached at cache_path; at most MAX_NEW_LOOKUPS uncached films are tried per
    run. Missing API key or per-film failures never raise.
    """
    api_key = config.get("TMDB_API_KEY")
    if not api_key:
        print("enrich: TMDB_API_KEY not configured; skipping enrichment",
              file=sys.stderr)
        return {}
    cache_path = pathlib.Path(cache_path)
    cache = _load_cache(cache_path)
    client = _Tmdb(api_key)

    # Distinct films, first-seen order; backfill year/director across showings.
    films: dict[str, dict] = {}
    for s in screenings:
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or "").strip()
        if not title:
            continue
        fk = s.get("film_key") or _title_key(title)
        film = films.setdefault(fk, {"title": title, "year": None, "director": None})
        if film["year"] is None and s.get("year"):
            film["year"] = s["year"]
        if film["director"] is None and s.get("director"):
            film["director"] = s["director"]

    out: dict[str, dict] = {}
    new_lookups = skipped_over_cap = failures = 0
    consecutive_failures = 0
    try:
        for fk, film in films.items():
            if _is_program(film["title"]):
                continue
            ck = _cache_key(fk, film["year"])
            if ck in cache:
                entry = cache[ck]
            else:
                if new_lookups >= MAX_NEW_LOOKUPS:
                    skipped_over_cap += 1
                    continue
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    skipped_over_cap += 1  # TMDB unreachable; catch up next run
                    continue
                new_lookups += 1
                try:
                    entry = _lookup(client, film["title"], film["year"],
                                    film["director"])
                except Exception as err:  # transient: log, do NOT cache as miss
                    failures += 1
                    consecutive_failures += 1
                    print(f"enrich: lookup failed for {film['title']!r}: {err}",
                          file=sys.stderr)
                    continue
                consecutive_failures = 0
                cache[ck] = entry
            if isinstance(entry, dict) and entry.get("tmdb_id"):
                out[fk] = entry
    finally:
        _save_cache(cache_path, cache)

    if skipped_over_cap:
        print(f"enrich: {skipped_over_cap} lookups deferred "
              f"(cap {MAX_NEW_LOOKUPS}/run); they'll catch up next run",
              file=sys.stderr)
    if failures:
        print(f"enrich: {failures} lookups failed (not cached; will retry)",
              file=sys.stderr)
    print(f"enrich: {len(out)}/{len(films)} films matched "
          f"({new_lookups} new lookups, {client.requests_made} API requests)",
          file=sys.stderr)
    return out


# --- Smoke test --------------------------------------------------------------

if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    data_path = root / "build" / "site" / "data.json"
    cache_file = root / "data" / "tmdb_cache.json"

    data = json.loads(data_path.read_text(encoding="utf-8"))
    screenings = data["screenings"]
    for s in screenings:  # older data.json rows predate film_key
        s.setdefault("film_key", _title_key(s.get("title") or ""))

    matched = enrich(screenings, cache_file)

    film_keys = {s["film_key"] for s in screenings if s.get("film_key")}
    programs = {s["film_key"] for s in screenings if _is_program(s.get("title") or "")}
    cache = _load_cache(cache_file)
    cached_keys = {k.rsplit("|", 1)[0] for k in cache}
    processed = {fk for fk in film_keys - programs if fk in cached_keys}
    misses = sorted(fk for fk in processed if fk not in matched)

    print(f"films in data.json:   {len(film_keys)}")
    print(f"program titles skipped: {len(programs)}")
    print(f"processed (cached):   {len(processed)}")
    print(f"matched:              {len(matched)}"
          + (f"  ({len(matched) / len(processed):.0%} of processed)" if processed else ""))
    print(f"with poster:          {sum(1 for m in matched.values() if m['poster'])}")
    print(f"with trailer:         {sum(1 for m in matched.values() if m['trailer'])}")

    print("\nexample matches:")
    shown = 0
    trailer_shown = False
    for fk, m in matched.items():
        if shown >= 5:
            break
        if shown == 4 and not trailer_shown and not m["trailer"]:
            continue  # make sure at least one example carries a trailer URL
        print(f"  {fk!r} -> #{m['tmdb_id']} {m['title']} ({m['year']}) "
              f"genres={m['genres']}")
        print(f"    poster:  {m['poster']}")
        print(f"    trailer: {m['trailer']}")
        trailer_shown = trailer_shown or bool(m["trailer"])
        shown += 1

    if misses:
        print(f"\nexample misses ({len(misses)} total):")
        for fk in misses[:5]:
            print(f"  {fk!r}")

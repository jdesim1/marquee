"""Turn RawScreenings from all sources into canonical screening dicts.

- Maps source venue spellings to registry slugs via data/venues.json aliases.
- Dedupes the same showtime reported by multiple sources; source priority
  decides which record's fields win.
"""
import hashlib
import re

from .model import RawScreening

# Higher wins when the same showtime arrives from multiple sources.
SOURCE_PRIORITY = {"repertory_nyc": 2, "screenslate": 1}

_punct = re.compile(r"[^a-z0-9]+")


def _key(s: str) -> str:
    return _punct.sub(" ", (s or "").casefold()).strip()


def build_alias_map(venues: dict) -> dict:
    """alias key -> slug, from names + aliases in the registry."""
    amap = {}
    for v in venues["venues"]:
        for name in [v["name"], *v.get("aliases", [])]:
            amap[_key(name)] = v["slug"]
    return amap


_FMT_CANON = [
    (re.compile(r"70\s*mm", re.I), "70mm"),
    (re.compile(r"35\s*mm", re.I), "35mm"),
    (re.compile(r"16\s*mm", re.I), "16mm"),
    (re.compile(r"8\s*mm", re.I), "8mm"),
    (re.compile(r"imax", re.I), "IMAX"),
    (re.compile(r"4k", re.I), "4K"),
    (re.compile(r"dcp", re.I), "DCP"),
    (re.compile(r"digital", re.I), "Digital"),
    (re.compile(r"video", re.I), "Video"),
]
_FMT_NOISE = re.compile(r"\$|admission|^\d+m$|ticket", re.I)


def clean_fmt(fmt: str | None) -> str | None:
    """Canonicalize dirty source format strings ('35MM', '35mm*', '4K RESTORATION');
    drop non-format noise ('General Admission: $17', '100m')."""
    if not fmt:
        return None
    seen = []
    for rx, canon in _FMT_CANON:
        if rx.search(fmt) and canon not in seen:
            seen.append(canon)
    if seen:
        # "4K RESTORATION" -> "4K"; "4K DCP" -> "4K DCP"; "35mm/DCP" -> "35mm/DCP"
        return ("/" if "/" in fmt else " ").join(seen)
    if _FMT_NOISE.search(fmt):
        return None
    return fmt.strip() or None


def _title_key(title: str) -> str:
    # Rep listings decorate titles ("… in 35mm", "…: 4K Restoration") — strip
    # trailing format/restoration tags so cross-source dedupe still matches.
    t = _key(title)
    t = re.sub(r"\b(in )?(35 ?mm|70 ?mm|16 ?mm|4k( restoration)?|imax)\b", " ", t).strip()
    return t or _key(title)


def normalize(raws: list[RawScreening], venues: dict) -> tuple[list[dict], list[str]]:
    """Returns (canonical screenings, unmatched venue_raw names)."""
    amap = build_alias_map(venues)
    unmatched = []
    best: dict[tuple, tuple[int, dict]] = {}

    for r in raws:
        if not r.title_raw or not r.date:
            continue
        slug = amap.get(_key(r.venue_raw))
        if slug is None:
            unmatched.append(r.venue_raw)
        # Source data bug seen live: release year leaking into runtime ("2025 min").
        year, runtime = r.year, r.runtime_min
        if runtime is not None and not 1 <= runtime <= 600:
            if year is None and 1890 <= runtime <= 2100:
                year = runtime
            runtime = None
        if year is not None and not 1890 <= year <= 2100:
            year = None
        dedupe_key = (slug or _key(r.venue_raw), r.date, r.time or "", _title_key(r.title_raw))
        sid = hashlib.sha1("|".join(map(str, dedupe_key)).encode()).hexdigest()[:16]
        rec = {
            "id": sid,
            "venue": slug,
            "venue_raw": r.venue_raw,
            "title": r.title_raw.strip(),
            "year": year,
            "director": r.director,
            "runtime_min": runtime,
            "format": clean_fmt(r.fmt),
            "date": r.date,
            "time": r.time,
            "ticket_url": r.ticket_url,
            "series": r.series,
            "source": r.source,
        }
        prio = SOURCE_PRIORITY.get(r.source, 0)
        kept = best.get(dedupe_key)
        if kept is None:
            best[dedupe_key] = (prio, rec)
        else:
            kept_prio, kept_rec = kept
            hi, lo = (rec, kept_rec) if prio > kept_prio else (kept_rec, rec)
            # Winner's fields, but backfill gaps from the loser.
            merged = {k: (hi[k] if hi[k] not in (None, "") else lo[k]) for k in hi}
            merged["id"] = hi["id"]
            merged["source"] = hi["source"]
            best[dedupe_key] = (max(prio, kept_prio), merged)

    out = sorted((rec for _, rec in best.values()),
                 key=lambda r: (r["date"], r["time"] or "99:99", r["venue"] or r["venue_raw"], r["title"]))
    return out, sorted(set(unmatched))

"""Turn RawScreenings from all sources into canonical screening dicts.

- Maps source venue spellings to registry slugs via data/venues.json aliases.
- Dedupes the same showtime reported by multiple sources; source priority
  decides which record's fields win.
"""
import hashlib
import re

from .model import RawScreening

# Higher wins when the same showtime arrives from multiple sources.
# Venue-direct beats curated aggregator beats daily-snapshot fill.
SOURCE_PRIORITY = {"repertory_nyc": 4, "alamo": 3, "screenslate": 2, "nyc_parks": 2, "poorstuart": 1}

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
    (re.compile(r"dolby\s*cinema", re.I), "Dolby Cinema"),
    (re.compile(r"atmos", re.I), "Dolby Atmos"),
    (re.compile(r"laser", re.I), "Laser"),
    (re.compile(r"rpx", re.I), "RPX"),
    (re.compile(r"4dx", re.I), "4DX"),
    (re.compile(r"screenx", re.I), "ScreenX"),
    (re.compile(r"\b3-?d\b", re.I), "3D"),
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


# Event decorations sources bolt onto the same film: "+ Q&A", "+ intro",
# "| Series Name". Strip for MATCHING only; display keeps the winner's title.
_PIPE_TAIL = re.compile(r"\s*\|\s.*$")
_EVENT_TAIL = re.compile(
    r"\s*[+&]\s*(with\s+)?(q\s*&?\s*a|intro\w*|discussion|panel|reception|conversation)\b.*$",
    re.I)


def _title_key(title: str) -> str:
    # Rep listings decorate titles ("… in 35mm", "… + Q&A", "…: 4K Restoration",
    # "… | Series") — strip decorations so cross-source dedupe matches.
    t = _PIPE_TAIL.sub("", title)
    t = _EVENT_TAIL.sub("", t)
    t = _key(t)
    t = re.sub(r"\b(in )?(35 ?mm|70 ?mm|16 ?mm|4k( restoration)?|imax)\b", " ", t).strip()
    # Articles vary freely between listings ("Director's Cut" / "The Director's Cut")
    t = " ".join(w for w in t.split() if w not in ("the", "a", "an"))
    return t or _key(title)


_SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "of", "in", "on",
                "at", "to", "for", "by", "with", "from", "as"}
_ROMAN = re.compile(r"^[ivxlcdm]{1,5}$")


def smart_title_case(t: str) -> str:
    """Title-case an all-caps/all-lower source title; keeps roman numerals."""
    words = t.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if _ROMAN.fullmatch(lw.rstrip(":,")) and i > 0:
            out.append(w.upper())
        elif 0 < i < len(words) - 1 and lw in _SMALL_WORDS:
            out.append(lw)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out)


def polish_titles(screenings: list[dict], films: dict) -> list[dict]:
    """Fix title casing: TMDB's official casing when the listing IS the plain
    film title (preserves BlacKkKlansman-style official styling); otherwise
    smart-title-case shouty/lowercase source titles. Decorated program titles
    ("Michael Mann's Manhunter: The Final Cut") are left intact."""
    for s in screenings:
        fe = films.get(s.get("film_key")) if films else None
        title = s.get("title") or ""
        if fe and fe.get("title") and _title_key(fe["title"]) == s.get("film_key"):
            s["title"] = fe["title"]
        elif title.isupper() or title.islower():
            s["title"] = smart_title_case(title)
    return screenings


def normalize(raws: list[RawScreening], venues: dict) -> tuple[list[dict], list[str]]:
    """Returns (canonical screenings, unmatched venue_raw names)."""
    amap = build_alias_map(venues)
    unmatched = []
    best: dict[tuple, tuple[int, dict]] = {}

    for r in raws:
        if not r.title_raw or not r.date:
            continue
        slug = amap.get(_key(r.venue_raw))
        if slug is None and not r.extra.get("outdoor"):
            # Outdoor hosts (parks, rooftops) are deliberately unregistered;
            # only report venues that look like missing aliases.
            unmatched.append(r.venue_raw)
        # Source data bug seen live: release year leaking into runtime ("2025 min").
        year, runtime = r.year, r.runtime_min
        if runtime is not None and not 1 <= runtime <= 600:
            if year is None and 1890 <= runtime <= 2100:
                year = runtime
            runtime = None
        if year is not None and not 1890 <= year <= 2100:
            year = None
        # "Title | Series Name" — move the pipe tail into series.
        title = r.title_raw.strip()
        series = r.series
        pipe = _PIPE_TAIL.search(title)
        if pipe:
            tail = pipe.group(0).strip(" |").strip()
            title = _PIPE_TAIL.sub("", title).strip()
            series = series or tail
        # Q&A/intro is a property of the SHOWING, not the film: strip it from
        # the display name, keep it as a per-screening tag.
        tail_m = _EVENT_TAIL.search(title)
        qa = bool(tail_m and re.search(r"q\s*&?\s*a", tail_m.group(0), re.I))
        se = r.extra.get("special_event")
        if isinstance(se, dict) and se.get("event_type") == "q_and_a":
            qa = True
        if tail_m:
            title = (_EVENT_TAIL.sub("", title).strip()) or title
        dedupe_key = (slug or _key(r.venue_raw), r.date, r.time or "", _title_key(r.title_raw))
        sid = hashlib.sha1("|".join(map(str, dedupe_key)).encode()).hexdigest()[:16]
        rec = {
            "id": sid,
            "venue": slug,
            "venue_raw": r.venue_raw,
            "title": title,
            "year": year,
            "director": r.director,
            "runtime_min": runtime,
            "format": clean_fmt(r.fmt),
            "date": r.date,
            "time": r.time,
            "ticket_url": r.ticket_url,
            "series": series,
            "source": r.source,
            "film_key": _title_key(r.title_raw),
            "tags": (["outdoor"] if r.extra.get("outdoor") else []) + (["qa"] if qa else []),
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
            merged["tags"] = sorted(set(hi.get("tags", [])) | set(lo.get("tags", [])))
            best[dedupe_key] = (max(prio, kept_prio), merged)

    # Second pass: the same film at the same venue+date with start times <=45
    # minutes apart is one show with drifted listings (sources — and their
    # upstreams — carry the same program twice under different names/times).
    def _mins(t):
        return int(t[:2]) * 60 + int(t[3:]) if t else None

    def _richness(prio, rec):
        meta = sum(1 for k in ("year", "director", "runtime_min", "format") if rec.get(k))
        return (prio, meta, len(rec.get("ticket_url") or ""))

    groups: dict[tuple, list] = {}
    for dkey, (prio, rec) in best.items():
        groups.setdefault((dkey[0], rec["date"], rec["film_key"]), []).append((dkey, prio, rec))

    def _flush(cluster):
        if len(cluster) < 2:
            return
        keep = max(cluster, key=lambda m: _richness(m[1], m[2]))
        for m in cluster:
            if m is keep:
                continue
            for k in keep[2]:
                if keep[2][k] in (None, "", []) and m[2].get(k) not in (None, "", []):
                    keep[2][k] = m[2][k]
            best.pop(m[0], None)

    for members in groups.values():
        if len(members) < 2:
            continue
        timed = sorted((m for m in members if m[2]["time"]), key=lambda m: _mins(m[2]["time"]))
        cluster = timed[:1]
        for m in timed[1:]:
            if _mins(m[2]["time"]) - _mins(cluster[-1][2]["time"]) <= 45:
                cluster.append(m)
            else:
                _flush(cluster)
                cluster = [m]
        _flush(cluster)

    # Third pass: one name per film — the best-attested title for a film_key
    # applies everywhere, so listings never show spelling variants side by side.
    title_pick: dict[str, tuple] = {}
    for prio, rec in best.values():
        fk = rec["film_key"]
        cand = (prio,
                sum(1 for k in ("year", "director", "runtime_min") if rec.get(k)),
                -len(rec["title"]))
        if fk not in title_pick or cand > title_pick[fk][0]:
            title_pick[fk] = (cand, rec["title"])
    for _, rec in best.values():
        rec["title"] = title_pick[rec["film_key"]][1]

    out = sorted((rec for _, rec in best.values()),
                 key=lambda r: (r["date"], r["time"] or "99:99", r["venue"] or r["venue_raw"], r["title"]))
    return out, sorted(set(unmatched))

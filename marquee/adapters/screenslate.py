"""Marquee adapter for Screen Slate (https://www.screenslate.com).

Screen Slate is the canonical NYC arthouse/repertory listings site. It runs
Drupal 10 with a fully open, unauthenticated JSON:API at
https://www.screenslate.com/jsonapi (verified live 2026-07-25).

How the data is shaped (learned by exploration):

- node--screening is one film-program's run at one venue. Its showtimes live
  in field_showtimes, a list of paragraph--showtime entities, each with
  field_time (ISO-8601 datetime WITH America/New_York offset, e.g.
  "2026-07-25T19:00:00-04:00") and an optional field_note ("Q&A with ...").
- field_media_titles is a list of paragraph--screening_media_title, each
  referencing node--media_title (title, field_year, field_runtime_minutes,
  field_director -> taxonomy_term--directors) and field_format ->
  taxonomy_term--formats ("35mm", "DCP", "Digital Video", ...). More than one
  media title means a double feature / shorts program.
- field_venue -> node--venue (title is the venue spelling; field_city ->
  taxonomy_term--cities). Screen Slate also covers the Bay Area now, so this
  adapter filters server-side to venues whose city is "New York".
- field_series -> node--series (title).
- field_url on the screening is the venue's EXTERNAL ticket page; the
  screening's own Screen Slate page is path.alias.

CRITICAL API QUIRK (why we sort instead of filter by date):
  Any JSON:API filter that touches paragraph entities silently returns an
  empty result set on this site (a known Drupal paragraphs/access behavior) —
  filter[x][condition][path]=field_showtimes.field_time matches nothing no
  matter the operator or value format, and even /jsonapi/paragraph/showtime
  with a filter on its own field_time returns []. Node-level filters
  (title, field_venue.field_city.name, ...) work fine, and SORTING through
  the paragraph relationship also works. So the strategy is:

    sort=-field_showtimes.field_time      (descending by showtime)
    filter[city][condition][path]=field_venue.field_city.name
    filter[city][condition][value]=New York

  and page forward until every node on a page has max(showtime) < start_date
  (the ordering is monotonic in each node's max showtime), filtering
  showtimes into [start, start+days) client-side. A handful of nodes with
  junk far-future dates (year 3035 etc.) sort first and are filtered out
  client-side like everything else. For a today+30d window this costs about
  9-10 requests at page[limit]=50.
"""

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from ..model import RawScreening

BASE = "https://www.screenslate.com"
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT = 20          # seconds per request
MAX_RETRIES = 2       # additional attempts after the first
MAX_REQUESTS = 20     # hard cap per run
PAGE_LIMIT = 50       # Drupal JSON:API maximum

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text):
    """Strip tags and collapse whitespace from a Drupal HTML text value."""
    if not text:
        return None
    import html as _html
    out = _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", text))).strip()
    return out or None


def _ssl_context():
    """Default verified context; add the macOS system CA bundle if the
    Python install has no certs of its own (python.org builds often don't)."""
    ctx = ssl.create_default_context()
    try:
        import os
        if not ctx.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
            ctx.load_verify_locations("/etc/ssl/cert.pem")
    except (OSError, ssl.SSLError):
        pass
    return ctx


_SSL_CTX = _ssl_context()


def _get(url, _state):
    """GET a JSON document politely: UA header, 20s timeout, 2 retries."""
    _state["requests"] += 1
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.api+json",
    })
    ctx = _SSL_CTX
    last_err = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))   # 2s, then 4s
    raise ConnectionError(f"Screen Slate unreachable: {url}: {last_err}")


def _collection_url(offset):
    """Build the screening-collection URL for one page."""
    params = [
        ("page[limit]", str(PAGE_LIMIT)),
        ("page[offset]", str(offset)),
        # Descending by showtime; monotonic in each node's max showtime.
        ("sort", "-field_showtimes.field_time"),
        # NYC only (Screen Slate also lists Bay Area venues).
        ("filter[city][condition][path]", "field_venue.field_city.name"),
        ("filter[city][condition][value]", "New York"),
        ("include", ",".join([
            "field_venue",
            "field_series",
            "field_showtimes",
            "field_media_titles.field_media_title.field_director",
            "field_media_titles.field_format",
        ])),
        # Sparse fieldsets keep payloads small (politeness).
        ("fields[node--screening]",
         "title,field_display_title,body,field_url,path,"
         "field_venue,field_series,field_showtimes,field_media_titles"),
        ("fields[node--venue]", "title"),
        ("fields[node--series]", "title"),
        ("fields[paragraph--showtime]", "field_time,field_note"),
        ("fields[paragraph--screening_media_title]", "field_media_title,field_format"),
        ("fields[node--media_title]",
         "title,field_year,field_runtime_minutes,field_director"),
        ("fields[taxonomy_term--formats]", "name"),
        ("fields[taxonomy_term--directors]", "name"),
    ]
    return BASE + "/jsonapi/node/screening?" + urllib.parse.urlencode(params)


def _rel_ids(node, field):
    """Return the list of related-resource ids for a relationship field."""
    data = (node.get("relationships") or {}).get(field, {}).get("data")
    if data is None:
        return []
    if isinstance(data, dict):
        return [data.get("id")]
    return [d.get("id") for d in data if isinstance(d, dict)]


def _film_info(node, included):
    """Resolve media titles: (films list, joined_fmt).

    Each film dict: {title, year, director, runtime_min, fmt}.
    """
    films = []
    for para_id in _rel_ids(node, "field_media_titles"):
        para = included.get(para_id)
        if not para:
            continue
        fmt_names = []
        for fid in _rel_ids(para, "field_format"):
            term = included.get(fid)
            if term:
                name = (term.get("attributes") or {}).get("name")
                if name:
                    fmt_names.append(name)
        film = {"title": None, "year": None, "director": None,
                "runtime_min": None, "fmt": "/".join(fmt_names) or None}
        mt_ids = _rel_ids(para, "field_media_title")
        mt = included.get(mt_ids[0]) if mt_ids else None
        if mt:
            attrs = mt.get("attributes") or {}
            film["title"] = attrs.get("title")
            year_raw = attrs.get("field_year")
            if year_raw:
                m = re.search(r"\d{4}", str(year_raw))
                film["year"] = int(m.group()) if m else None
            rt = attrs.get("field_runtime_minutes")
            film["runtime_min"] = int(rt) if isinstance(rt, (int, float)) else None
            directors = []
            for did in _rel_ids(mt, "field_director"):
                term = included.get(did)
                if term:
                    name = (term.get("attributes") or {}).get("name")
                    if name:
                        directors.append(name)
            film["director"] = ", ".join(directors) or None
        if film["title"] or film["fmt"]:
            films.append(film)
    all_fmts = []
    for f in films:
        if f["fmt"] and f["fmt"] not in all_fmts:
            all_fmts.append(f["fmt"])
    return films, ("/".join(all_fmts) or None)


def fetch(start_date: str, days: int = 30) -> list[RawScreening]:
    """Fetch NYC screenings from Screen Slate for [start_date, start_date+days).

    One RawScreening per showtime. Raises ConnectionError only if the site is
    entirely unreachable; malformed records are skipped.
    """
    start = date.fromisoformat(start_date)
    end = start + timedelta(days=days)          # exclusive
    state = {"requests": 0}
    results = []
    seen_showtimes = set()   # (venue, title, date, time) exact-slot dedupe
    seen_nodes = set()       # join-sort can duplicate a node across page edges
    offset = 0
    truncated = False

    while True:
        if state["requests"] >= MAX_REQUESTS:
            truncated = True
            break
        doc = _get(_collection_url(offset), state)
        data = doc.get("data") or []
        included = {i["id"]: i for i in doc.get("included") or []}
        page_done = False   # saw a node whose max showtime < start

        for node in data:
            try:
                node_id = node.get("id")
                times = []
                for st_id in _rel_ids(node, "field_showtimes"):
                    st = included.get(st_id)
                    if not st:
                        continue
                    t = (st.get("attributes") or {}).get("field_time")
                    if t:
                        times.append((t, st_id, st))
                if times and max(t[0] for t in times)[:10] < start.isoformat():
                    # Ordering is monotonic in max showtime: nothing at or
                    # after this node can reach the window anymore.
                    page_done = True
                if not times or node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)

                attrs = node.get("attributes") or {}
                venue_ids = _rel_ids(node, "field_venue")
                venue = included.get(venue_ids[0]) if venue_ids else None
                if not venue:
                    continue   # can't place a screening without its venue
                venue_name = ((venue.get("attributes") or {}).get("title") or "").strip()
                if not venue_name:
                    continue

                series_ids = _rel_ids(node, "field_series")
                series_node = included.get(series_ids[0]) if series_ids else None
                series = ((series_node.get("attributes") or {}).get("title")
                          if series_node else None)

                films, joined_fmt = _film_info(node, included)
                year = director = runtime = None
                if len(films) == 1:
                    title = films[0]["title"] or attrs.get("field_display_title") \
                        or attrs.get("title") or ""
                    year = films[0]["year"]
                    director = films[0]["director"]
                    runtime = films[0]["runtime_min"]
                else:
                    # Multi-film program (double feature / shorts) or no
                    # linked media titles. The bare node title is an admin
                    # label with venue suffixes ("... ff", "... moma"), so
                    # prefer the editorial display title, then the joined
                    # film titles.
                    title = attrs.get("field_display_title") \
                        or " + ".join(f["title"] for f in films if f["title"]) \
                        or attrs.get("title") or ""
                title = _strip_html(title)
                if not title:
                    continue

                alias = (attrs.get("path") or {}).get("alias")
                page_url = BASE + alias if alias else None
                ext = (attrs.get("field_url") or {}).get("uri") \
                    if isinstance(attrs.get("field_url"), dict) else None

                extra = {}
                if page_url:
                    extra["url"] = page_url
                desc = _strip_html((attrs.get("body") or {}).get("value")
                                   if isinstance(attrs.get("body"), dict) else None)
                if desc:
                    extra["description"] = desc[:500]
                if len(films) > 1:
                    extra["films"] = films

                for t_str, st_id, st in times:
                    try:
                        dt = datetime.fromisoformat(t_str)
                    except ValueError:
                        continue
                    # field_time carries the America/New_York offset, so the
                    # literal components ARE the local wall-clock showtime.
                    d = dt.date()
                    if not (start <= d < end):
                        continue
                    # The source contains occasional editorial duplicates
                    # (twin nodes with "-0" aliases; the same showtime
                    # entered twice on one node) — dedupe exact slots.
                    key = (venue_name, title, d.isoformat(), dt.strftime("%H:%M"))
                    if key in seen_showtimes:
                        continue
                    seen_showtimes.add(key)
                    note = _strip_html((st.get("attributes") or {}).get("field_note"))
                    results.append(RawScreening(
                        source="screenslate",
                        venue_raw=venue_name,
                        title_raw=title,
                        date=d.isoformat(),
                        time=dt.strftime("%H:%M"),
                        year=year,
                        director=director,
                        runtime_min=runtime,
                        fmt=joined_fmt,
                        ticket_url=ext or page_url,
                        series=series,
                        notes=note,
                        extra=dict(extra),
                    ))
            except Exception:
                continue   # one malformed node must not kill the run

        if page_done or not doc.get("links", {}).get("next") or not data:
            break
        offset += PAGE_LIMIT

    if truncated and results:
        # Descending traversal: with the request cap hit, the LATER part of
        # the window is what got covered.
        covered_from = min(r.date for r in results)
        for r in results:
            r.extra["window_truncated_before"] = covered_from

    results.sort(key=lambda r: (r.date, r.time or "", r.venue_raw, r.title_raw))
    return results


if __name__ == "__main__":
    today = date.today().isoformat()
    screenings = fetch(today, days=7)
    print(f"screenslate: {len(screenings)} showtimes, "
          f"{today} +7d, {len({s.venue_raw for s in screenings})} venues")
    for s in screenings[:3]:
        print(f"  {s.date} {s.time}  {s.title_raw!r} ({s.year}, {s.director}) "
              f"[{s.fmt}] @ {s.venue_raw}  series={s.series!r} note={s.notes!r}")

"""Adapter for NYC Parks outdoor movie events — via NYC Open Data (Socrata).

Dataset w3wp-dpdi "NYC Parks Public Events – Upcoming 14 Days" (probed live
2026-08-17): official, openly licensed, refreshed daily — the commercially
clean route to Parks data (nycgovparks.org itself sits behind a bot wall).

    GET https://data.cityofnewyork.us/resource/w3wp-dpdi.json
        ?$where=upper(categories) like '%MOVIE%' OR upper(categories) like '%FILM%'

Row shape: title, guid, startdate, starttime, enddate, endtime, categories
("Film | Free Summer Movies | Movies Under the Stars"), parknames,
coordinates ("lat, lon"), link.url, description, pubdate.

Source quirks (verified live):
- The horizon is a rolling ~14 days regardless of the asked window; the
  store's first_seen/last_seen accumulation plus the 4x-daily cron keeps
  coverage continuous.
- starttime/endtime are full datetimes whose DATE half is the refresh date,
  not the event date (52/56 rows disagreed with startdate) — only their
  HH:MM is meaningful. startdate is the true event date.
- Film titles are embedded in event titles: "Movies Under the Stars:
  Project Hail Mary (2026) - Rated PG-13" -> series "Movies Under the
  Stars", title "Project Hail Mary", year 2026. Titles with no colon
  ("Bryant Park Movie Nights") pass through whole.

Park names are deliberately NOT in the venue registry — outdoor one-off
hosts stay unregistered, and null-venue rows pass filters by design.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from ..model import RawScreening

DATASET_URL = "https://data.cityofnewyork.us/resource/w3wp-dpdi.json"
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT_S = 30
MAX_RETRIES = 2
BACKOFF_BASE_S = 2.0
ROW_LIMIT = 1000  # dataset holds ~2 weeks of all-category events (~1-2k rows)


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Verifying SSL context that works on python.org macOS builds (empty CA
    store until "Install Certificates.command"); falls back to certifi or a
    system bundle. Verification is never disabled."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        candidates: list[str] = []
        try:
            import certifi
            candidates.append(certifi.where())
        except ImportError:
            pass
        candidates += [
            "/etc/ssl/cert.pem",
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
        ]
        for cafile in candidates:
            if os.path.isfile(cafile):
                ctx = ssl.create_default_context(cafile=cafile)
                break
    _SSL_CONTEXT = ctx
    return ctx


def _get_json(url: str):
    """GET a URL, return parsed JSON. Retries with backoff; raises on final failure."""
    last_err: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            _time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S,
                                        context=_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
            last_err = err
    raise RuntimeError(f"nyc_parks request failed after {1 + MAX_RETRIES} attempts: {url}") from last_err


def _clean_str(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


_YEAR = re.compile(r"\s*\((19|20)\d{2}\)\s*")
_RATED = re.compile(r"\s*[-–—]\s*Rated\b.*$", re.I)


def _parse_title(event_title: str) -> tuple[str, str | None, int | None]:
    """'Movies Under the Stars: Project Hail Mary (2026) - Rated PG-13'
    -> ('Project Hail Mary', 'Movies Under the Stars', 2026).
    No colon -> the whole event title, no series, no year."""
    series, _, film = event_title.partition(": ")
    if not film.strip():
        return event_title.strip(), None, None
    film = _RATED.sub("", film)
    year = None
    m = _YEAR.search(film)
    if m:
        year = int(m.group(0).strip().strip("()"))
        film = _YEAR.sub(" ", film)
    film = film.strip(" -–—").strip()
    if not film:
        return event_title.strip(), None, None
    return film, series.strip() or None, year


def fetch(start_date: str, days: int = 60) -> list[RawScreening]:
    """Fetch Parks movie/film events for `days` days starting at `start_date`.

    One request. The source only ever holds a rolling ~14-day window, so the
    effective horizon is min(days, whatever the dataset carries).
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as err:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from err
    end = start + timedelta(days=max(1, int(days)) - 1)

    params = urllib.parse.urlencode({
        "$where": "upper(categories) like '%MOVIE%' OR upper(categories) like '%FILM%'",
        "$limit": str(ROW_LIMIT),
    })
    payload = _get_json(f"{DATASET_URL}?{params}")
    if not isinstance(payload, list):
        raise RuntimeError("nyc_parks: unexpected payload shape")

    results: list[RawScreening] = []
    seen_ids: set[str] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        event_title = _clean_str(row.get("title"))
        startdate = _clean_str(row.get("startdate"))
        venue = _clean_str(row.get("parknames"))
        if not event_title or not startdate or not venue:
            continue
        try:
            day = datetime.strptime(startdate[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not start <= day <= end:
            continue

        guid = _clean_str(row.get("guid"))
        if guid:
            if guid in seen_ids:
                continue
            seen_ids.add(guid)

        # starttime's date half is the refresh date, not the event date —
        # only HH:MM is trustworthy.
        time_hhmm = None
        st = _clean_str(row.get("starttime"))
        if st:
            m = re.search(r"\b(\d{2}):(\d{2}):\d{2}\b", st)
            if m:
                time_hhmm = f"{m.group(1)}:{m.group(2)}"

        title, series, year = _parse_title(event_title)

        extra: dict = {"outdoor": True}
        if guid:
            extra["source_id"] = guid
        cats = _clean_str(row.get("categories"))
        if cats:
            extra["categories"] = cats
        coords = _clean_str(row.get("coordinates"))
        if coords and "," in coords:
            lat_s, _, lon_s = coords.partition(",")
            try:
                extra["lat"], extra["lon"] = float(lat_s), float(lon_s)
            except ValueError:
                pass
        link = row.get("link")
        url = _clean_str(link.get("url")) if isinstance(link, dict) else _clean_str(link)

        results.append(RawScreening(
            source="nyc_parks",
            venue_raw=venue,
            title_raw=title,
            date=day.isoformat(),
            time=time_hhmm,
            year=year,
            ticket_url=url,
            series=series,
            notes="Free" if (cats and "Free Summer Movies" in cats) else None,
            extra=extra,
        ))
    return results


if __name__ == "__main__":
    today = date.today().isoformat()
    screenings = fetch(today, days=14)
    print(f"nyc_parks: {len(screenings)} screenings, {today} +14 days")
    venues = sorted({s.venue_raw for s in screenings})
    print(f"parks seen ({len(venues)}): {', '.join(venues)}")
    for s in screenings[:5]:
        print(s)

"""Adapter for Alamo Drafthouse NYC — unofficial-but-tolerated market feed.

Single unauthenticated JSON endpoint (probed live 2026-08-17):

    GET https://drafthouse.com/s/mother/v2/schedule/market/nyc
        -> {"data": {presentations, sessions, market, formats, ...}}

- market[0].cinemas: the NYC locations ({id, slug, name, status}); venue_raw
  is emitted as "Alamo Drafthouse {name}", which resolves via the registry
  ("Alamo Drafthouse Lower Manhattan" / "Staten Island" / an alias for
  "Downtown Brooklyn").
- presentations: one per show/event; show.title is clean text; superTitle
  (when present) is a collection name ("Movie Parties", "Kids Camp") -> series.
- sessions: one per showtime; showTimeClt is wall-clock America/New_York
  (we take its calendar date, so a 12:30am late show lands on the day it
  actually screens); status ONSALE/SOLDOUT/PAST.
- formats: slug -> display title ("2d-digital" -> "2D Digital"); passed as
  fmt and canonicalized downstream by normalize.clean_fmt.

No year/director/runtime in the feed — normalize's dedupe backfills those
from other sources when the same screening is also listed elsewhere.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from ..model import RawScreening

FEED_URL = "https://drafthouse.com/s/mother/v2/schedule/market/nyc"
SHOW_URL = "https://drafthouse.com/nyc/show/{slug}"
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT_S = 30
MAX_RETRIES = 2
BACKOFF_BASE_S = 2.0
KEEP_STATUSES = {"ONSALE", "SOLDOUT"}


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
    raise RuntimeError(f"alamo request failed after {1 + MAX_RETRIES} attempts: {url}") from last_err


def _clean_str(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def fetch(start_date: str, days: int = 60) -> list[RawScreening]:
    """Fetch NYC Alamo sessions for `days` days starting at `start_date`.

    One request; the feed carries the full on-sale horizon (~4 months out).
    Sessions outside the window, PAST, or hidden are dropped.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as err:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from err
    end = start + timedelta(days=max(1, int(days)) - 1)

    payload = _get_json(FEED_URL)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("alamo: unexpected payload shape (no data object)")

    cinemas: dict[str, str] = {}
    for m in data.get("market") or []:
        for c in m.get("cinemas") or []:
            cid, name = _clean_str(c.get("id")), _clean_str(c.get("name"))
            if cid and name:
                cinemas[cid] = f"Alamo Drafthouse {name}"

    fmt_titles = {f.get("slug"): _clean_str(f.get("title"))
                  for f in data.get("formats") or [] if isinstance(f, dict)}

    pres: dict[str, dict] = {}
    for p in data.get("presentations") or []:
        if not isinstance(p, dict) or p.get("isHidden"):
            continue
        slug = _clean_str(p.get("slug"))
        if slug:
            pres[slug] = p

    results: list[RawScreening] = []
    seen_ids: set[str] = set()
    for s in data.get("sessions") or []:
        if not isinstance(s, dict) or s.get("isHidden"):
            continue
        if s.get("status") not in KEEP_STATUSES:
            continue
        venue = cinemas.get(_clean_str(s.get("cinemaId")) or "")
        p = pres.get(_clean_str(s.get("presentationSlug")) or "")
        if venue is None or p is None:
            continue
        show = p.get("show") or {}
        title = _clean_str(show.get("title"))
        clt = _clean_str(s.get("showTimeClt"))  # "YYYY-MM-DDTHH:MM:SS" local
        if not title or not clt:
            continue
        try:
            when = datetime.strptime(clt, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if not start <= when.date() <= end:
            continue

        sid = _clean_str(s.get("sessionId"))
        if sid:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

        sup = p.get("superTitle") or {}
        series = _clean_str(sup.get("superTitle")) if isinstance(sup, dict) else None
        fmt_slug = _clean_str(s.get("formatSlug"))
        fmt = fmt_titles.get(fmt_slug) or fmt_slug

        extra: dict = {}
        if sid:
            extra["source_id"] = sid
        posters = show.get("posterImages") or []
        if posters and isinstance(posters[0], dict) and _clean_str(posters[0].get("uri")):
            extra["poster"] = posters[0]["uri"].strip()
        if s.get("status") == "SOLDOUT":
            extra["soldout"] = True

        slug = _clean_str(p.get("slug"))
        results.append(RawScreening(
            source="alamo",
            venue_raw=venue,
            title_raw=title,
            date=when.date().isoformat(),
            time=when.strftime("%H:%M"),
            fmt=fmt,
            ticket_url=SHOW_URL.format(slug=slug) if slug else None,
            series=series,
            notes="Sold out" if s.get("status") == "SOLDOUT" else None,
            extra=extra,
        ))
    return results


if __name__ == "__main__":
    today = date.today().isoformat()
    screenings = fetch(today, days=14)
    print(f"alamo: {len(screenings)} screenings, {today} +14 days")
    venues = sorted({s.venue_raw for s in screenings})
    print(f"venues seen ({len(venues)}): {', '.join(venues)}")
    for s in screenings[:3]:
        print(s)

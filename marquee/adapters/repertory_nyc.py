"""Adapter for repertory.nyc — NYC repertory-film aggregator.

Open, unauthenticated JSON API (probed live 2026-07-25):

    GET /api/theaters
        -> [{id, name, slug, address, website, borough, latitude, longitude}, ...]
    GET /api/screenings?date=YYYY-MM-DD&limit=N
        -> flat screening records (see below)
    GET /api/screenings/week?start=YYYY-MM-DD&end=YYYY-MM-DD
        -> same flat records for the inclusive date range; accepts arbitrary
           spans (verified: a 60-day span returned byte-identical id sets to
           chunked calls, no pagination/truncation observed at ~1400 records)

Screening record shape (all keys always present, values may be null):
    id, film_title, film_year, film_director, film_runtime_minutes,
    film_poster_url, theater_name, theater_slug, date ("YYYY-MM-DD"),
    time ("HH:MM" 24h), format, ticket_url, special_event

`special_event` is null or an object:
    {id, event_type, guests: [...], description}

We fetch via /screenings/week in 14-day chunks — small enough to dodge any
undocumented response cap, few enough to cover 60 days in 5 requests
(hard cap: MAX_REQUESTS per run).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from ..model import RawScreening

BASE_URL = "https://www.repertory.nyc/api"
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT_S = 20
MAX_RETRIES = 2          # retries beyond the first attempt
BACKOFF_BASE_S = 2.0     # sleep 2s, then 4s
CHUNK_DAYS = 14          # inclusive days covered per /week request
MAX_REQUESTS = 15        # hard cap per run


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
    raise RuntimeError(f"repertory.nyc request failed after {1 + MAX_RETRIES} attempts: {url}") from last_err


def _coerce_int(value) -> int | None:
    """Best-effort int coercion; None for anything non-numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _clean_str(value) -> str | None:
    """Stripped non-empty string, else None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _norm_time(value) -> str | None:
    """Normalize to 'HH:MM' 24h; None if unparseable."""
    value = _clean_str(value)
    if value is None:
        return None
    for fmt_ in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt_).strftime("%H:%M")
        except ValueError:
            continue
    return None


def _norm_date(value) -> str | None:
    """Validate 'YYYY-MM-DD'; None if unparseable."""
    value = _clean_str(value)
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _to_raw(rec: dict) -> RawScreening | None:
    """Map one API record to RawScreening; None if the record is unusable."""
    title = _clean_str(rec.get("film_title"))
    date_str = _norm_date(rec.get("date"))
    venue = _clean_str(rec.get("theater_name"))
    if not title or not date_str or not venue:
        return None

    extra: dict = {}
    slug = _clean_str(rec.get("theater_slug"))
    if slug:
        extra["venue_slug"] = slug
    poster = _clean_str(rec.get("film_poster_url"))
    if poster:
        extra["poster"] = poster
    source_id = _clean_str(rec.get("id"))
    if source_id:
        extra["source_id"] = source_id

    notes = None
    special = rec.get("special_event")
    if isinstance(special, dict):
        extra["special_event"] = special
        notes = (_clean_str(special.get("description"))
                 or _clean_str(special.get("event_type")))
    elif special:  # unexpected truthy scalar — keep the information anyway
        extra["special_event"] = special
        notes = _clean_str(str(special))

    return RawScreening(
        source="repertory_nyc",
        venue_raw=venue,
        title_raw=title,
        date=date_str,
        time=_norm_time(rec.get("time")),
        year=_coerce_int(rec.get("film_year")),
        director=_clean_str(rec.get("film_director")),
        runtime_min=_coerce_int(rec.get("film_runtime_minutes")),
        fmt=_clean_str(rec.get("format")),
        ticket_url=_clean_str(rec.get("ticket_url")),
        series=None,
        notes=notes,
        extra=extra,
    )


def fetch(start_date: str, days: int = 60) -> list[RawScreening]:
    """Fetch screenings for `days` days starting at `start_date` ("YYYY-MM-DD").

    Uses /api/screenings/week in CHUNK_DAYS-day windows (at most MAX_REQUESTS
    requests; the horizon is truncated if it would exceed that). Malformed
    records are skipped. Raises RuntimeError only if nothing is reachable.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as err:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from err
    days = max(1, int(days))
    if days > CHUNK_DAYS * MAX_REQUESTS:
        print(f"repertory_nyc: truncating horizon from {days} to "
              f"{CHUNK_DAYS * MAX_REQUESTS} days ({MAX_REQUESTS}-request cap)",
              file=sys.stderr)
        days = CHUNK_DAYS * MAX_REQUESTS
    end = start + timedelta(days=days - 1)

    results: list[RawScreening] = []
    seen_ids: set[str] = set()
    any_success = False
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
        params = urllib.parse.urlencode(
            {"start": chunk_start.isoformat(), "end": chunk_end.isoformat()})
        url = f"{BASE_URL}/screenings/week?{params}"
        try:
            payload = _get_json(url)
        except RuntimeError as err:
            if not any_success and chunk_start == start:
                raise  # first request dead -> source unreachable
            print(f"repertory_nyc: skipping chunk {chunk_start}..{chunk_end}: {err}",
                  file=sys.stderr)
            chunk_start = chunk_end + timedelta(days=1)
            continue
        any_success = True
        if not isinstance(payload, list):
            print(f"repertory_nyc: unexpected payload shape for {url}", file=sys.stderr)
            chunk_start = chunk_end + timedelta(days=1)
            continue
        for rec in payload:
            if not isinstance(rec, dict):
                continue
            try:
                raw = _to_raw(rec)
            except Exception as err:  # one bad record must not kill the run
                print(f"repertory_nyc: skipping record ({err}): {rec.get('id')}",
                      file=sys.stderr)
                continue
            if raw is None:
                continue
            rid = raw.extra.get("source_id")
            if rid:
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
            results.append(raw)
        chunk_start = chunk_end + timedelta(days=1)

    if not any_success:
        raise RuntimeError("repertory.nyc entirely unreachable")
    return results


if __name__ == "__main__":
    today = date.today().isoformat()
    screenings = fetch(today, days=14)
    print(f"repertory_nyc: {len(screenings)} screenings, {today} +14 days")
    venues = sorted({s.venue_raw for s in screenings})
    print(f"venues seen ({len(venues)}): {', '.join(venues)}")
    for s in screenings[:3]:
        print(s)

"""Adapter for Poor Stuart's Guide — NYC citywide showtimes BY THEATER.

The only source listing NYC CHAIN showtimes (AMC, Regal/UA, Alamo) alongside
arthouse houses. Server-rendered WordPress HTML, regenerated daily, and it
covers TODAY ONLY — so this adapter emits a single-day snapshot and returns
[] when the requested window does not include the page's date.

Probed live 2026-08-17. Page structure (machine-generated, very regular;
all inside <div class='entry-content'>):

    <span class="dateline">For Monday, August 17th, 2026</span>
    <div class='theaterblock'>
        <span class='theatername'>AMC Empire 25</span>
        <span class='theaterinfo'>234 West 42nd St, New York, NY<BR>
            ... phone / tickets / map / website buttons
                (class="ticketsButton", anchor text distinguishes them) ...
    <div class='movielisting'>            (repeats until next theaterblock)
        [<a href='metacritic...'>]<span class='acclaim|favorable|mixed|
            unfavorable'>NN</span>[</a>]  or  <span class='noscore'>00</span>
        <span class='movietitle'>TITLE</span> &nbsp; PG-13 &nbsp; <br>
        <span class="showtimestypelabel">Regular Showtimes (Reserved Seating
            / Open Caption)</span> ... <span class="showtimes-timesitem">
            2:00pm\n4:50pm\n7:30pm</span> ...   (label+times pairs repeat)

65 theaters on probe day; six are New Jersey (Paramus / Jersey City / Wayne /
Cranford / Skillman / Montclair) and are skipped — detected by "NJ" in the
theaterinfo address line, never by name (AMC Loews Orpheum 7's address line
carries no state at all, so the default is include).

Quirk: AMC premium-format labels arrive mangled as "XL atam C" /
"PRIME atam C" (really "XL at AMC" / "Prime at AMC"); we repair that in fmt.

Fetch discipline: this is one person's WordPress site. ONE page fetch per
run; only if the HTML page entirely fails do we try the open WP REST API
(/wp-json/wp/v2/pages?slug=...) once as a fallback — max two endpoints.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import ssl
import sys
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..model import RawScreening

NYC = ZoneInfo("America/New_York")


def _today_nyc() -> date:
    # CI runners live in UTC; "today" must always mean New York's today.
    return datetime.now(NYC).date()

PAGE_URL = "https://www.poorstuart.com/nyc-movie-showtimes-citywide-by-theater/"
REST_URL = ("https://www.poorstuart.com/wp-json/wp/v2/pages"
            "?slug=nyc-movie-showtimes-citywide-by-theater&_fields=content,modified")
USER_AGENT = "Marquee/0.1 (personal NYC moviegoing tool)"
TIMEOUT_S = 20
MAX_RETRIES = 2          # retries beyond the first attempt, per endpoint
BACKOFF_BASE_S = 2.0     # sleep 2s, then 4s

# Attribute noise inside the "(...)" part of a showtimes label — seating and
# ticketing details that aren't worth carrying into notes. Meaningful bits
# (Open Caption, languages, subtitles, dubbing) pass through.
_ATTR_DROP = {
    "reserved seating", "recliner seats", "no passes", "closed caption",
    "laser projection",
}

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

_RATINGS = {"G", "PG", "PG-13", "R", "NC-17", "NC17", "NR", "UR", "UNRATED"}


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


def _get(url: str) -> str:
    """GET a URL, return decoded body. Retries with backoff; raises on failure."""
    last_err: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            _time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S,
                                        context=_ssl_context()) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last_err = err
    raise RuntimeError(
        f"poorstuart request failed after {1 + MAX_RETRIES} attempts: {url}"
    ) from last_err


def _fetch_html() -> str:
    """Fetch the page HTML: the rendered page first, WP REST as sole fallback."""
    try:
        return _get(PAGE_URL)
    except RuntimeError as err:
        print(f"poorstuart: page fetch failed ({err}); trying WP REST fallback",
              file=sys.stderr)
    body = _get(REST_URL)  # raises RuntimeError if this fails too
    try:
        pages = json.loads(body)
        rendered = pages[0]["content"]["rendered"]
        if isinstance(rendered, str) and rendered:
            return rendered
    except (json.JSONDecodeError, LookupError, TypeError) as err:
        raise RuntimeError(f"poorstuart: WP REST fallback unusable: {err}") from err
    raise RuntimeError("poorstuart: WP REST fallback returned empty content")


def _text(fragment: str) -> str:
    """Tags stripped, entities unescaped, whitespace collapsed."""
    frag = re.sub(r"<[^>]+>", " ", fragment)
    frag = _html.unescape(frag).replace("\xa0", " ")
    return re.sub(r"\s+", " ", frag).strip()


def _page_date(html: str) -> str | None:
    """Parse the dateline ('For Monday, August 17th, 2026') -> 'YYYY-MM-DD'."""
    m = re.search(r'<span class=["\']dateline["\']>(.*?)</span>', html, re.S)
    if not m:
        return None
    dm = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
                   _text(m.group(1)))
    if not dm:
        return None
    month = _MONTHS.get(dm.group(1).lower())
    if not month:
        return None
    try:
        return date(int(dm.group(3)), month, int(dm.group(2))).isoformat()
    except ValueError:
        return None


_TITLE_FMT_RX = re.compile(r"\b(35 ?mm|70 ?mm|16 ?mm|dcp|4k)\b", re.I)


def _fmt_from_title(title: str) -> str | None:
    """Arthouse titles carry the format ('The Odyssey (dcp)', 'Crank - 35mm');
    surface it as fmt (normalize.clean_fmt canonicalizes downstream)."""
    m = _TITLE_FMT_RX.search(title)
    return m.group(1) if m else None


def _to_24h(hh: str, mm: str, ap: str) -> str | None:
    hour, minute = int(hh), int(mm)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour %= 12
    if ap.lower() == "p":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _venue_ticket_url(head: str) -> str | None:
    """The theaterblock's 'tickets' button href (else 'website'); never tel:."""
    anchors = re.findall(r"<a\b([^>]*)>(.*?)</a>", head, re.S)
    by_label: dict[str, str] = {}
    for attrs, inner in anchors:
        href = re.search(r"""href=["']([^"']+)["']""", attrs)
        if not href or href.group(1).startswith("tel:"):
            continue
        by_label.setdefault(_text(inner).lower(), href.group(1))
    return by_label.get("tickets") or by_label.get("website")


def _split_label(label: str) -> tuple[str | None, str | None]:
    """'3D / RPX Showtimes (No Passes / Open Caption)' -> (fmt, notes).

    fmt: the prefix before 'Showtimes' (None for 'Regular'); repairs the
    site's mangled 'atam C' -> 'at AMC'. notes: parenthetical attributes
    minus seating/ticketing noise.
    """
    label = _text(label)
    m = re.match(r"^(.*?)\s*Showtimes\s*(?:\((.*)\))?\s*$", label)
    if not m:
        return None, label or None
    prefix = m.group(1).strip().replace("atam C", "at AMC")
    fmt = prefix if prefix and prefix.lower() != "regular" else None
    # split ONLY on space-flanked slashes: "Japanese w/ Eng Subtitles" is one
    # attribute, "Reserved Seating / Open Caption" is two
    kept = [a.strip() for a in re.split(r"\s+/\s+", m.group(2) or "")
            if a.strip() and a.strip().lower() not in _ATTR_DROP]
    if fmt is None and "4K" in kept:  # "Regular Showtimes (4K / ...)"
        fmt = "4K"
        kept.remove("4K")
    return fmt, " / ".join(kept) or None


def _parse_listing(chunk: str, venue: str, day: str, ticket_url: str | None,
                   address: str | None) -> list[RawScreening]:
    """One <div class='movielisting'> chunk -> screenings (may be several)."""
    tm = re.search(r"<span class=['\"]movietitle['\"]>(.*?)</span>", chunk, re.S)
    if not tm:
        return []
    title = _text(tm.group(1))
    if not title:
        return []

    extra_base: dict = {}
    if address:
        extra_base["address"] = address
    sm = re.search(
        r"<span class=['\"](?:acclaim|favorable|mixed|unfavorable)['\"]>(\d+)</span>",
        chunk)
    if sm:
        extra_base["metacritic"] = int(sm.group(1))
    # MPAA rating and runtime ("88MIN") sit as bare text between the title
    # span and the first <br>
    after_title = chunk[tm.end():]
    head_text = _text(after_title.split("<br", 1)[0])
    for token in head_text.split():
        if token.upper() in _RATINGS:
            extra_base["rating"] = token
            break
    runtime = None
    rm = re.search(r"\b(\d{1,3})\s*MIN\b", head_text, re.I)
    if rm:
        runtime = int(rm.group(1))

    out: list[RawScreening] = []

    def emit(raw_item: str, fmt: str | None, base_notes: str | None) -> None:
        # a timesitem may embed marker spans ("opencaptions" OC, "allages");
        # the (.*?)</span> capture stops inside them, but the times and the
        # class names both land in raw_item, which is all we need
        parts = [base_notes] if base_notes else []
        if "opencaptions" in raw_item and "Open Caption" not in parts:
            parts.append("Open Caption")
        if "allages" in raw_item:
            parts.append("All Ages")
        notes = " / ".join(parts) or None
        for hh, mm, ap in re.findall(r"(\d{1,2}):(\d{2})\s*([apAP])\.?[mM]",
                                     _text(raw_item)):
            t24 = _to_24h(hh, mm, ap)
            if t24 is None:
                continue
            out.append(RawScreening(
                source="poorstuart",
                venue_raw=venue,
                title_raw=title,
                date=day,
                time=t24,
                runtime_min=runtime,
                fmt=fmt or _fmt_from_title(title),
                ticket_url=ticket_url,
                notes=notes,
                extra=dict(extra_base),
            ))

    _item_rx = re.compile(
        r"<span class=['\"]showtimes-timesitem['\"]>(.*?)</span>", re.S)
    sections = re.split(r"<span class=['\"]showtimestypelabel['\"]>", after_title)
    # arthouse-style listings (Nitehawk, ...) have NO labels: bare timesitems
    # right after the title — they land in sections[0]
    items0 = _item_rx.findall(sections[0])
    for item in items0:
        emit(item, None, None)
    if len(sections) == 1 and not items0:
        # Roxy-style: no labels, no timesitem spans — times are bare text
        # after the first <br> ("... 88MIN &nbsp; <br> 7:00PM")
        emit(after_title.split("<br", 1)[-1], None, None)
    for section in sections[1:]:
        label, _, rest = section.partition("</span>")
        fmt, base_notes = _split_label(label)
        for item in _item_rx.findall(rest):
            emit(item, fmt, base_notes)
    return out


def _parse(html: str, day: str) -> list[RawScreening]:
    """Parse the whole page into screenings dated `day`."""
    results: list[RawScreening] = []
    seen: set[tuple] = set()
    sections = re.split(r"<div class=['\"]theaterblock['\"]>", html)
    for section in sections[1:]:
        try:
            nm = re.search(r"<span class=['\"]theatername['\"]>(.*?)</span>",
                           section, re.S)
            if not nm:
                continue
            venue = _text(nm.group(1))
            if not venue:
                continue
            listings = re.split(r"<div class=['\"]movielisting['\"]>", section)
            head = listings[0]  # theatername + theaterinfo block
            address = None
            im = re.search(r"<span class=['\"]theaterinfo['\"]>(.*?)<br", head,
                           re.S | re.I)
            if im:
                address = _text(im.group(1)) or None
            if address and re.search(r"\bNJ\b", address):
                continue  # New Jersey — five boroughs only
            ticket_url = _venue_ticket_url(head)
            for chunk in listings[1:]:
                try:
                    for s in _parse_listing(chunk, venue, day, ticket_url, address):
                        key = (s.venue_raw, s.title_raw, s.time, s.fmt, s.notes)
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append(s)
                except Exception as err:  # one bad listing must not kill the run
                    print(f"poorstuart: skipping a listing at {venue!r}: {err}",
                          file=sys.stderr)
        except Exception as err:  # nor one bad theater section
            print(f"poorstuart: skipping a theater section: {err}", file=sys.stderr)
    return results


def fetch(start_date: str, days: int = 60) -> list[RawScreening]:
    """Today-only snapshot: screenings for the page's date (today), or [].

    Poor Stuart's publishes only the current day, so the horizon is ignored
    beyond checking that [start_date, start_date+days) includes the page's
    date. If start_date is after today, no request is made at all.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as err:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from err
    days = max(1, int(days))
    today = _today_nyc()
    if start > today:
        return []

    html = _fetch_html()
    day = _page_date(html)
    if day is None:
        print("poorstuart: dateline not found/parsed; assuming today",
              file=sys.stderr)
        day = today.isoformat()
    if not (start.isoformat() <= day <= (start + timedelta(days=days - 1)).isoformat()):
        print(f"poorstuart: page is for {day}, outside requested window; "
              f"emitting nothing", file=sys.stderr)
        return []
    results = _parse(html, day)
    if not results:
        print("poorstuart: page fetched but zero screenings parsed — "
              "layout may have changed", file=sys.stderr)
    return results


if __name__ == "__main__":
    today = _today_nyc().isoformat()
    screenings = fetch(today)
    print(f"poorstuart: {len(screenings)} screenings for {today}")
    venues = sorted({s.venue_raw for s in screenings})
    print(f"venues seen ({len(venues)}):")
    for v in venues:
        print(f"  {v}")
    for s in screenings[:3]:
        print(s)

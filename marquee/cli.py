"""Marquee pipeline runner: fetch -> normalize -> store -> build site.

Usage (from the project root):
    python3 -m marquee            # full run, 60-day horizon
    python3 -m marquee --days 14
"""
import argparse
import datetime
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

NYC = ZoneInfo("America/New_York")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main(argv=None):
    p = argparse.ArgumentParser(prog="marquee")
    p.add_argument("--days", type=int, default=60, help="horizon in days (default 60)")
    p.add_argument("--start", default=None, help="start date YYYY-MM-DD (default today)")
    args = p.parse_args(argv)
    # CI runners live in UTC; the moviegoing day is New York's.
    start = args.start or datetime.datetime.now(NYC).date().isoformat()

    from .adapters import alamo, nyc_parks, poorstuart, repertory_nyc, screenslate
    from . import enrich, normalize, store, build_site

    venues = json.loads((ROOT / "data" / "venues.json").read_text())

    raws = []
    for mod in (repertory_nyc, screenslate, alamo, nyc_parks, poorstuart):
        name = mod.__name__.rsplit(".", 1)[-1]
        try:
            got = mod.fetch(start, args.days)
            print(f"[marquee] {name}: {len(got)} raw screenings", file=sys.stderr)
            raws.extend(got)
        except Exception as e:  # one source down must not kill the run
            print(f"[marquee] WARNING: {name} failed entirely: {e}", file=sys.stderr)

    canon, unmatched = normalize.normalize(raws, venues)
    if unmatched:
        print(f"[marquee] unmatched venue names ({len(unmatched)}): {unmatched}", file=sys.stderr)

    db = store.Store(ROOT / "marquee.db")
    screenings = db.upsert(canon)

    try:
        films = enrich.enrich(screenings, ROOT / "data" / "tmdb_cache.json")
        print(f"[marquee] enrich: {len(films)} films matched to TMDB", file=sys.stderr)
    except Exception as e:  # enrichment is additive — never fatal
        print(f"[marquee] WARNING: enrich failed: {e}", file=sys.stderr)
        films = {}

    site_dir = ROOT / "build" / "site"
    generated_at = datetime.datetime.now(NYC).strftime("%Y-%m-%d %H:%M")
    build_site.build(site_dir, venues["venues"], screenings, generated_at, films)
    print(f"[marquee] {len(screenings)} screenings -> {site_dir / 'index.html'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

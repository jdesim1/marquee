# Marquee

Every screen in New York, one place. A personal tool that consolidates NYC movie
showtimes — indie/rep/doc houses first, chains and pop-ups next — with
time-of-day views and (soon) iPhone push alerts when favorite films get booked.

## Run

```bash
python3 -m marquee --days 60
```

Fetches live sources → normalizes/dedupes → upserts into `marquee.db` (SQLite,
tracks `first_seen` per screening for the future alert diff) → writes the
browsable site to `build/site/index.html` + `data.json`. Stdlib only, no
dependencies.

Serve locally:

```bash
python3 -m http.server 8714 --directory build/site
```

## Layout

- `marquee/adapters/` — one module per source; each exposes `fetch(start_date, days) -> list[RawScreening]`
- `marquee/normalize.py` — venue alias mapping (`data/venues.json`) + cross-source dedupe (repertory.nyc wins over Screen Slate)
- `marquee/store.py` — SQLite upserts; `new_since()` is the phase-3 alert feed
- `marquee/build_site.py` — emits the static site (self-contained, phone-first)
- `data/venues.json` — venue registry: slugs, boroughs, categories, alias spellings

## Phases

1. **Foundation (this)** — repertory.nyc + Screen Slate, filters + saved views
2. Full field — Poor Stuart chain fill, NYC Parks outdoor, Alamo, festivals timeline, film cards (trailer/scores/awards)
3. Alerts — Letterboxd watchlist sync, diff engine, ntfy push
4. Long tail — venue-direct scrapers, newsletters, freshness monitoring

Full scope: the "Marquee — Scope & Source Catalog" artifact (see project memory).

Sources are small organizations. Be gentle: one fetch per source per run.
Support [Screen Slate](https://www.screenslate.com) — this leans on their work.

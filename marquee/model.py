"""Shared data model for Marquee adapters and pipeline."""
from dataclasses import dataclass, field


@dataclass
class RawScreening:
    """One showtime as reported by a single source, before normalization.

    All dates/times are America/New_York local, as published by the venue.
    Adapters emit venue names exactly as the source spells them; normalize.py
    maps them to registry slugs via the alias table in data/venues.json.
    """

    source: str                     # "repertory_nyc" | "screenslate" | ...
    venue_raw: str                  # venue name as the source spells it
    title_raw: str
    date: str                       # "YYYY-MM-DD"
    time: str | None                # "HH:MM" 24-hour, None if source has no time
    year: int | None = None
    director: str | None = None
    runtime_min: int | None = None
    fmt: str | None = None          # "35mm", "70mm", "DCP", "16mm", ...
    ticket_url: str | None = None
    series: str | None = None       # series/program name if any
    notes: str | None = None
    extra: dict = field(default_factory=dict)  # source-specific leftovers

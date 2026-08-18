"""Emit the static site: data.json + the Ticket Stub design.

The page template (site_template.html, chosen from three commissioned
directions) is fully static and reads ./data.json at runtime, so building
the site is: write the data, copy the template.
"""
import json
import pathlib

TEMPLATE = pathlib.Path(__file__).resolve().parent / "site_template.html"


def build(site_dir, venues, screenings, generated_at, films=None):
    site_dir = pathlib.Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "data.json").write_text(json.dumps(
        {"generated_at": generated_at, "venues": venues, "screenings": screenings,
         "films": films or {}},
        separators=(",", ":"), default=str))
    (site_dir / "index.html").write_text(
        TEMPLATE.read_text().replace("__MARQUEE_BUILD__", generated_at))

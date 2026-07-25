"""SQLite store. Tracks first_seen per screening so the future alert engine
can diff "what got booked since last run" — recorded from day one on purpose.
"""
import datetime
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS screenings (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,          -- canonical screening dict as JSON
    date TEXT NOT NULL,          -- denormalized for pruning/queries
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screenings_date ON screenings(date);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def upsert(self, screenings: list[dict]) -> list[dict]:
        """Upsert this run's screenings; returns them with first_seen attached.

        Rows not seen in this run keep their history (past screenings stay);
        the site is built from the current run only.
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        out = []
        with self.db:
            for s in screenings:
                row = self.db.execute(
                    "SELECT first_seen FROM screenings WHERE id = ?", (s["id"],)
                ).fetchone()
                first_seen = row[0] if row else now
                self.db.execute(
                    "INSERT INTO screenings (id, data, date, first_seen, last_seen) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data, date=excluded.date, last_seen=excluded.last_seen",
                    (s["id"], json.dumps(s), s["date"], first_seen, now),
                )
                out.append({**s, "first_seen": first_seen})
        return out

    def new_since(self, iso_timestamp: str) -> list[dict]:
        """Screenings first seen after the given time — the alert feed (phase 3)."""
        rows = self.db.execute(
            "SELECT data, first_seen FROM screenings WHERE first_seen > ? ORDER BY first_seen",
            (iso_timestamp,),
        ).fetchall()
        return [{**json.loads(d), "first_seen": fs} for d, fs in rows]

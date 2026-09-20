#!/usr/bin/env python3
"""Rebuild data/processed/tracker.db from every snapshot in data/raw/snapshots/.

The database is a derived artifact. This script drops and rebuilds it from
scratch on every run rather than incrementally updating it, which means the
snapshots stay the single source of truth: if the schema changes, or a bug is
found in how a column is computed, the fix is to change this file and re-run --
never to hand-edit the database or a snapshot.

Two tables:

  listings   one row per posting id ever seen open, carrying the most recent
             field values, plus first_seen_at / last_seen_at -- computed here,
             not supplied by the feed. Those two are the whole point: the feed
             has no "when did this open" or "when did this close" field, so
             first/last appearance in our own snapshots is the only way to get
             them.

  pull_log   one row per snapshot, with the open/close deltas against the
             previous snapshot. This is what turns a pile of daily files into a
             trend line.

A note on gaps: no_longer_active_count compares each snapshot against the
*previous snapshot present*, not against "yesterday". If a scheduled run is
missed, the next run's delta spans that gap; days_since_prev makes that visible
so the analysis can account for it instead of reading a two-day gap as a spike.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "raw" / "snapshots"
DB_PATH = REPO_ROOT / "data" / "processed" / "tracker.db"


def display(path: Path) -> str:
    """Repo-relative path when it is inside the repo, absolute otherwise.

    --out-dir / --db can point anywhere (tests, backfills), so relative_to()
    cannot be assumed to succeed.
    """
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)

SCHEMA = """
DROP TABLE IF EXISTS listings;
DROP TABLE IF EXISTS pull_log;

CREATE TABLE listings (
    id             TEXT PRIMARY KEY,
    company_name   TEXT,
    title          TEXT,
    category       TEXT,
    locations      TEXT,  -- JSON array as text
    terms          TEXT,  -- JSON array as text
    sponsorship    TEXT,
    url            TEXT,
    date_posted    INTEGER,  -- unix seconds, from the feed
    date_updated   INTEGER,  -- unix seconds, from the feed
    active         INTEGER,
    first_seen_at  TEXT NOT NULL,  -- YYYY-MM-DD, computed from our snapshots
    last_seen_at   TEXT NOT NULL,  -- YYYY-MM-DD, computed from our snapshots
    days_seen      INTEGER NOT NULL
);

CREATE INDEX idx_listings_company    ON listings(company_name);
CREATE INDEX idx_listings_category   ON listings(category);
CREATE INDEX idx_listings_first_seen ON listings(first_seen_at);
CREATE INDEX idx_listings_last_seen  ON listings(last_seen_at);

CREATE TABLE pull_log (
    snapshot_date          TEXT PRIMARY KEY,  -- YYYY-MM-DD
    pulled_at              TEXT,              -- ISO8601 UTC, when fetch.py ran
    total_seen             INTEGER NOT NULL,  -- open listings in our slice
    new_count              INTEGER NOT NULL,  -- ids never seen in any earlier snapshot
    no_longer_active_count INTEGER NOT NULL,  -- ids in the previous snapshot, gone here
    days_since_prev        INTEGER,           -- NULL on the first snapshot
    source_total           INTEGER,           -- whole feed, before filtering
    source_active          INTEGER            -- whole feed, active only
);
"""


def load_snapshots(snapshot_dir: Path) -> list[tuple[str, dict]]:
    """Return (date, payload) pairs sorted by date."""
    out = []
    for path in sorted(snapshot_dir.glob("*.json")):
        day = path.stem
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {path.name} is not valid JSON: {exc}")
        if not isinstance(payload, dict) or "listings" not in payload:
            raise SystemExit(f"error: {path.name} is not a snapshot (no 'listings' key)")
        out.append((day, payload))
    return out


def days_between(a: str, b: str) -> int:
    from datetime import date

    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def build(snapshot_dir: Path, db_path: Path) -> int:
    snapshots = load_snapshots(snapshot_dir)
    if not snapshots:
        print(f"error: no snapshots found in {snapshot_dir}", file=sys.stderr)
        return 1

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)

        # id -> row, accumulated across snapshots in date order so that later
        # snapshots overwrite the mutable fields while first_seen_at sticks.
        listings: dict[str, dict] = {}
        pull_rows = []
        prev_ids: set[str] | None = None
        prev_day: str | None = None
        ever_seen: set[str] = set()

        for day, payload in snapshots:
            ids = set()
            for item in payload["listings"]:
                lid = item.get("id")
                if not lid:
                    continue
                ids.add(lid)
                row = listings.get(lid)
                first_seen = row["first_seen_at"] if row else day
                days_seen = (row["days_seen"] + 1) if row else 1
                listings[lid] = {
                    "id": lid,
                    "company_name": item.get("company_name"),
                    "title": item.get("title"),
                    "category": item.get("category"),
                    "locations": json.dumps(item.get("locations") or []),
                    "terms": json.dumps(item.get("terms") or []),
                    "sponsorship": item.get("sponsorship"),
                    "url": item.get("url"),
                    "date_posted": item.get("date_posted"),
                    "date_updated": item.get("date_updated"),
                    "active": 1 if item.get("active") else 0,
                    "first_seen_at": first_seen,
                    "last_seen_at": day,
                    "days_seen": days_seen,
                }

            new_count = len(ids - ever_seen)
            gone = len(prev_ids - ids) if prev_ids is not None else 0
            ever_seen |= ids

            pull_rows.append(
                (
                    day,
                    payload.get("fetched_at"),
                    len(ids),
                    new_count,
                    gone,
                    days_between(prev_day, day) if prev_day else None,
                    payload.get("source_total"),
                    payload.get("source_active"),
                )
            )
            prev_ids, prev_day = ids, day

        cols = list(next(iter(listings.values())).keys())
        conn.executemany(
            f"INSERT INTO listings ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in listings.values()],
        )
        conn.executemany(
            "INSERT INTO pull_log (snapshot_date, pulled_at, total_seen, new_count, "
            "no_longer_active_count, days_since_prev, source_total, source_active) "
            "VALUES (?,?,?,?,?,?,?,?)",
            pull_rows,
        )
        conn.commit()
    finally:
        conn.close()

    print(
        f"built {display(db_path)} from {len(snapshots)} snapshot(s) "
        f"({snapshots[0][0]} .. {snapshots[-1][0]}): "
        f"{len(listings)} distinct listings, {len(pull_rows)} pull_log rows"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)
    return build(args.snapshot_dir, args.db)


if __name__ == "__main__":
    raise SystemExit(main())

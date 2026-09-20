#!/usr/bin/env python3
"""Checks build_db.py's day-over-day logic, which a single snapshot can't exercise.

Real snapshots accumulate one per day, so the interesting cases -- a listing
appearing, disappearing, reappearing, and a missed run leaving a gap -- would
take a week of calendar time to observe naturally. This builds three synthetic
snapshots out of slices of a real one and asserts the counts exactly.

Run:  python tests/test_build_db.py
"""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "raw" / "snapshots"


def main() -> int:
    real_files = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not real_files:
        print("error: need at least one real snapshot; run scripts/fetch.py first", file=sys.stderr)
        return 1
    real = json.loads(real_files[-1].read_text())
    listings = real["listings"]
    if len(listings) < 200:
        print(f"error: need >=200 listings in {real_files[-1].name}", file=sys.stderr)
        return 1

    def snapshot(items, fetched_at):
        s = copy.deepcopy(real)
        s["listings"], s["count"], s["fetched_at"] = items, len(items), fetched_at
        return s

    with tempfile.TemporaryDirectory() as tmp:
        snaps = Path(tmp) / "snapshots"
        snaps.mkdir()
        # Overlapping slices: day2 adds 80 and drops 50; day4 skips a day.
        plan = {
            "2026-09-01": listings[0:100],
            "2026-09-02": listings[50:180],
            "2026-09-04": listings[80:180] + listings[180:200],
        }
        for day, items in plan.items():
            (snaps / f"{day}.json").write_text(json.dumps(snapshot(items, f"{day}T06:00:00+00:00")))

        db = Path(tmp) / "test.db"
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "build_db.py"),
             "--snapshot-dir", str(snaps), "--db", str(db)],
            capture_output=True, text=True,
        )
        if proc.returncode:
            print(proc.stdout + proc.stderr, file=sys.stderr)
            return 1

        conn = sqlite3.connect(db)
        failures = []

        def check(label, got, want):
            status = "ok  " if got == want else "FAIL"
            if got != want:
                failures.append(label)
            print(f"  {status} {label}: got={got}" + ("" if got == want else f" want={want}"))

        print("pull_log:")
        rows = conn.execute(
            "SELECT snapshot_date, total_seen, new_count, no_longer_active_count, "
            "days_since_prev FROM pull_log ORDER BY snapshot_date"
        ).fetchall()
        for row, want in zip(rows, [
            ("2026-09-01", 100, 100, 0, None),   # first snapshot: everything is new, nothing gone
            ("2026-09-02", 130, 80, 50, 1),      # 80 open, 50 close
            ("2026-09-04", 120, 20, 30, 2),      # gap day recorded as days_since_prev=2
        ]):
            check(want[0], row, want)

        print("listings:")
        check("distinct ids", conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 200)

        def seen(i):
            return conn.execute(
                "SELECT first_seen_at, last_seen_at, days_seen FROM listings WHERE id=?",
                (listings[i]["id"],),
            ).fetchone()

        check("day 1 only",           seen(0),   ("2026-09-01", "2026-09-01", 1))
        check("days 1-2, then closed", seen(60),  ("2026-09-01", "2026-09-02", 2))
        check("open on all three",    seen(85),  ("2026-09-01", "2026-09-04", 3))
        check("first seen on day 2",  seen(100), ("2026-09-02", "2026-09-04", 2))
        check("first seen on day 4",  seen(190), ("2026-09-04", "2026-09-04", 1))
        conn.close()

    if failures:
        print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

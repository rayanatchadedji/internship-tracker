#!/usr/bin/env python3
"""Pull the SimplifyJobs Summer 2027 listings feed, filter it, save today's snapshot.

Writes data/raw/snapshots/YYYY-MM-DD.json. Snapshots are immutable inputs to
build_db.py -- never edited after the fact, only added to. Re-running on the
same day overwrites that day's file (so a manual workflow_dispatch re-run is
safe and does not create a second row for one calendar day).

Two filters are applied, both deliberate:

  1. category -- keep only the role families actually being applied to. Matched
     as a substring so the source's variant spellings are caught too
     ("Software Engineering", "Data Science, AI & Machine Learning").

  2. active -- keep only currently-open postings. This one matters more than it
     looks. The source feed is a cumulative dump: it keeps every posting it has
     ever seen (16,962 entries, of which ~4,400 are open) and flips `active` to
     false rather than removing the row. Snapshotting everything would mean
     every listing appears in every snapshot forever, which makes last_seen_at
     always equal to today and makes "this closed" unobservable. Snapshotting
     only the open ones is what makes first_seen_at / last_seen_at, and the
     open/close counts in pull_log, mean anything at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SOURCE_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json"
)

# Substrings, not exact matches -- see module docstring.
CATEGORY_MATCHES = ("AI/ML/Data", "Software", "Data Science")

# Fields carried into the snapshot. The feed also ships source, company_url,
# is_visible and degrees; none are used downstream and they roughly double the
# file size, so they are dropped at the door.
KEEP_FIELDS = (
    "id",
    "company_name",
    "title",
    "category",
    "terms",
    "locations",
    "sponsorship",
    "url",
    "date_posted",
    "date_updated",
    "active",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "raw" / "snapshots"


def display(path: Path) -> str:
    """Repo-relative path when it is inside the repo, absolute otherwise.

    --out-dir / --db can point anywhere (tests, backfills), so relative_to()
    cannot be assumed to succeed.
    """
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def download(url: str, timeout: int = 60) -> list[dict]:
    resp = requests.get(
        url, timeout=timeout, headers={"User-Agent": "internship-tracker/1.0"}
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list at {url}, got {type(payload).__name__}")
    return payload


def wanted(listing: dict) -> bool:
    if not listing.get("active"):
        return False
    category = listing.get("category") or ""
    return any(m in category for m in CATEGORY_MATCHES)


def trim(listing: dict) -> dict:
    return {k: listing.get(k) for k in KEEP_FIELDS}


def build_snapshot(raw: list[dict]) -> dict:
    kept = sorted((trim(x) for x in raw if wanted(x)), key=lambda x: x["id"] or "")
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_url": SOURCE_URL,
        "filter": {"categories": list(CATEGORY_MATCHES), "active_only": True},
        # Pre-filter totals, kept for provenance: they let the analysis say how
        # the tracked slice moved relative to the whole feed.
        "source_total": len(raw),
        "source_active": sum(1 for x in raw if x.get("active")),
        "count": len(kept),
        "listings": kept,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date",
        help="snapshot date as YYYY-MM-DD (default: today, UTC). For backfills/tests.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=SNAPSHOT_DIR, help="snapshot directory"
    )
    args = parser.parse_args(argv)

    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {day!r}", file=sys.stderr)
        return 2

    try:
        raw = download(SOURCE_URL)
    except (requests.RequestException, ValueError) as exc:
        # Exit non-zero so a bad upstream day shows up as a red CI run rather
        # than silently committing an empty or truncated snapshot.
        print(f"error: fetch failed: {exc}", file=sys.stderr)
        return 1

    snapshot = build_snapshot(raw)
    if snapshot["count"] == 0:
        print("error: 0 listings matched the filter -- refusing to write a snapshot", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{day}.json"
    existed = out_path.exists()
    # separators= keeps the file compact; the structure is stable and sorted, so
    # git stores day-over-day snapshots as small deltas.
    out_path.write_text(json.dumps(snapshot, separators=(",", ":")) + "\n")

    print(
        f"{'rewrote' if existed else 'wrote'} {display(out_path)}  "
        f"kept {snapshot['count']} of {snapshot['source_active']} active "
        f"({snapshot['source_total']} total in feed)  "
        f"{out_path.stat().st_size / 1_000_000:.2f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

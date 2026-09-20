# Summer 2027 Internship Tracker

A daily pipeline that snapshots open Software / AI-ML-Data / Data Science internship
postings, accumulates them into a queryable history, and answers questions the
source feed cannot: when did this posting open, when did it close, how long do
listings stay up, and which companies are actually hiring right now.

The source publishes a *current state* dump. This repo turns that into a *time
series* by keeping every day's pull as an immutable file and deriving history
from the sequence.

## How it works

```
 GitHub Actions (daily, 08:17 UTC)
        |
        v
 scripts/fetch.py  -->  data/raw/snapshots/YYYY-MM-DD.json   (committed, immutable)
                                    |
                                    v
                        scripts/build_db.py
                                    |
                                    v
                    data/processed/tracker.db   (derived, gitignored)
                                    |
                                    v
                        notebooks/01_trends.ipynb
```

Snapshots are the source of truth. The database is a derived artifact: it is
dropped and rebuilt from scratch on every `build_db.py` run, and it is
gitignored. If a column turns out to be computed wrong, the fix is to change
`build_db.py` and re-run — never to hand-edit the database or a snapshot.

## Data source

`https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json`

No auth, no signup, updated roughly hourly. At the time of writing it carried
16,962 postings, 4,394 of them open.

### What gets kept, and why

Two filters, both deliberate:

**Category** — only `AI/ML/Data`, `Software`, and `Data Science`. Hardware,
Quant and Product are skipped. This tracks the roles actually being applied to
rather than the whole firehose, which makes every number in the analysis mean
something specific. Matching is by substring, because the feed carries variant
spellings for the same families (`Software Engineering`, `Data Science, AI &
Machine Learning`) that an exact match would silently drop.

**Open postings only** — this one matters more than it looks. The feed is
*cumulative*: it keeps every posting it has ever seen and flips `active` to
`false` rather than removing the row. Snapshotting everything would mean every
listing appears in every snapshot forever, so `last_seen_at` would always equal
today and a posting closing would be unobservable. Keeping only open postings is
what makes `first_seen_at` / `last_seen_at` and the open/close counts mean
anything.

That leaves roughly 3,100 postings per day, about 1.25 MB of JSON. Day-over-day
files are near-identical, so git stores them as small deltas rather than 1.25 MB
of new objects per day.

Fields the pipeline does not use (`source`, `company_url`, `is_visible`,
`degrees`) are dropped at fetch time; they roughly double the file size.

### Known limitation: sponsorship

`sponsorship` is effectively unusable. Across the full feed it reads:

| value | count |
|---|---|
| `Other` (unspecified) | 16,856 |
| `Does Not Offer Sponsorship` | 57 |
| `U.S. Citizenship is Required` | 26 |
| `Offers Sponsorship` | 23 |

99.4% is `Other`. The column is carried through so it's there if the feed ever
starts populating it, but any sponsorship breakdown built on it today would be
describing 0.6% of the data. It is not used in the analysis.

## Schema

**`listings`** — one row per posting ever seen open.

| column | source |
|---|---|
| `id`, `company_name`, `title`, `category`, `locations`, `terms`, `sponsorship`, `url`, `date_posted`, `date_updated`, `active` | from the feed |
| `first_seen_at` | **computed** — first snapshot date this id appeared in |
| `last_seen_at` | **computed** — most recent snapshot date this id appeared in |
| `days_seen` | **computed** — number of snapshots containing this id |

`first_seen_at` / `last_seen_at` are the point of the whole exercise. The feed
has no "opened on" or "closed on" field, so first and last appearance in our own
snapshots is the only way to get them. A listing whose `last_seen_at` is older
than the newest snapshot date has dropped out of the feed — almost certainly
closed.

**`pull_log`** — one row per snapshot.

| column | meaning |
|---|---|
| `snapshot_date` | `YYYY-MM-DD` |
| `pulled_at` | ISO8601 UTC, when `fetch.py` actually ran |
| `total_seen` | open postings in the tracked slice |
| `new_count` | ids never seen in any earlier snapshot |
| `no_longer_active_count` | ids in the previous snapshot, absent from this one |
| `days_since_prev` | gap to the previous snapshot; `NULL` on the first |
| `source_total`, `source_active` | whole-feed counts before filtering |

`days_since_prev` exists because scheduled runs get missed. Deltas are computed
against the *previous snapshot present*, not against "yesterday", so a run that
skips a day produces a two-day delta. Without that column the analysis would
read the gap as a spike.

## Local use

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/fetch.py       # writes today's snapshot
python scripts/build_db.py    # rebuilds tracker.db from all snapshots
python tests/test_build_db.py # checks the day-over-day logic
```

Re-running `fetch.py` on the same day overwrites that day's file rather than
creating a second one, so a manual re-run is safe.

For the notebook: `pip install -r requirements-dev.txt`.

### Example queries

```sql
-- opened in the last 7 days
SELECT company_name, title, first_seen_at FROM listings
WHERE first_seen_at >= date('now', '-7 days')
ORDER BY first_seen_at DESC;

-- probably closed: last seen before the most recent snapshot
SELECT company_name, title, first_seen_at, last_seen_at,
       julianday(last_seen_at) - julianday(first_seen_at) AS days_open
FROM listings
WHERE last_seen_at < (SELECT MAX(snapshot_date) FROM pull_log)
ORDER BY last_seen_at DESC;

-- daily churn
SELECT snapshot_date, total_seen, new_count, no_longer_active_count
FROM pull_log ORDER BY snapshot_date;
```

## The scheduled run

`.github/workflows/daily-pull.yml` runs `fetch.py` daily, validates the result
by rebuilding the database, and commits the new snapshot back to the repo. It
also accepts `workflow_dispatch`, so a run can be triggered by hand from the
Actions tab to test it end to end instead of waiting for the cron.

Two things worth knowing about GitHub's scheduled workflows:

- **Cron is best-effort.** Runs are queued and can be delayed, sometimes by a
  lot. Nothing here assumes an exact run time.
- **GitHub disables scheduled workflows after 60 days of repository
  inactivity**, and emails first. Any commit re-arms it. Since this workflow
  commits daily, it keeps itself alive — but if the pipeline breaks and stops
  committing, the 60-day clock starts.

## Status

Pipeline built and tested against live data. History starts accumulating from
the first scheduled run; the analysis notebook needs several days of real
snapshots before it has a trend to show.

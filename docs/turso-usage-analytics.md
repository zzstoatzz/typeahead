# turso usage analytics — reading the read budget

How to pull usage/cost analytics straight from the `turso` CLI, what each
command reveals, and — most importantly — how to use `--queries`
attribution as a **lagging signal** on whether our architecture is
drifting toward corpus-proportional work.

The dashboard graphs (Rows Read / Rows Written / Syncs / Storage, last 30
days) are pretty but they only tell you *that* a number moved. The CLI
tells you *which query moved it*. That's the difference between "reads
spiked on May 16" and "the stats aggregate is a full table scan and runs
hourly."

## the three commands

All read-only, all hit the control plane (`api.turso.tech`), all use the
CLI's browser-auth session — no tokens to plumb. Numbers are
**period-to-date** for the current billing window (resets monthly).

### 1. `turso plan show` — org-wide quota dashboard

The closest CLI equivalent to the dashboard's headline tiles.

```
$ turso plan show
Organization: personal
Plan: scaler
Overages enabled

RESOURCE        USED     LIMIT       LIMIT %  OVERAGE
storage         3.6 GB   24 GB       15%
rows read       4996.6M  100000M     5%
rows written    45.2M    100M        45%
embedded syncs  6.3 GB   24 GB       26%
databases       4        1000000000  0%
locations       1        6           17%
groups          0        6           0%

Quota will reset on Sun, 31 May 2026 19:00:00 CDT
```

Use it for: "are we anywhere near a quota wall, and when does the meter
reset." On Scaler the answer is almost always "no" — at this snapshot
we're at 5% of reads and 45% of writes. Writes are the tighter budget.

### 2. `turso db inspect <db> [--verbose]` — per-database totals

Same metrics scoped to one database; `--verbose` breaks out by location.

```
$ turso db inspect typeahead --verbose
Total space used: 3.2 GB
Number of rows read: 3293904957
Number of rows written: 45005088
Embedded syncs: 6.3 GB

LOCATION       TYPE     ROWS READ   ROWS WRITTEN  TOTAL STORAGE  BYTES SYNCED
aws-us-east-1  primary  3293904957  45005088      3.2 GB         6.3 GB
```

Use it for: attributing the org total to a specific database (we have 4).
`typeahead` alone is 3.29B of the 5.0B org reads.

### 3. `turso db inspect <db> --queries` — **per-query attribution** ⭐

The one the dashboard can't do. Lists each distinct query (parameterized,
so all bind-value variants collapse to one row) with the rows it read and
wrote over the period. This is where you find the expensive patterns.

```
$ turso db inspect typeahead --queries
```

Snapshot 2026-05-24, top read consumers:

| rows read | query | what it is |
|---|---|---|
| **102.0M** | `SELECT … FROM actors WHERE handle != '' AND rowid > ? ORDER BY rowid LIMIT 2000` | local-replica **sync pagination** walking the whole table |
| **74.6M** | `SELECT COUNT(*), SUM(CASE…) FROM actors` | **stats aggregate**, full scan every run |
| **48.2M** | (10-column variant of the sync walk) | older/parallel sync path |
| **18.6M** | `… WHERE handle IN ('bad-example.com','aoc.bsky.social','zzstoatzz.io')` | ad-hoc **freshness probes** doing full scans |

Writes attribute cleanly too: the `last_activity_at` batch UPDATEs and the
`actors` upsert dominate, which is expected for an ingester.

## the gap: no daily time series

The CLI gives **point-in-time / period-to-date totals**. It does *not*
expose the day-by-day series behind the dashboard graphs — there's no
`--daily` / `--since` flag. If we ever need the time series
programmatically (e.g. to alert on a read-rate inflection), that lives in
the platform usage API the dashboard consumes, not the CLI. For "where is
the budget going right now," `--queries` is strictly more useful than the
graphs.

## why this is a lagging signal on our design

Our redesign principle is **"no request path may do work proportional to
corpus size."** `--queries` is the audit that tells us whether we're
actually holding that line, because a corpus-proportional pattern shows up
as a query whose `rows read` grows roughly linearly with the actor count
(~5.9M today) every time it runs.

Read the table above through that lens:

- **The 102M + 48M sync walks are corpus-proportional by design.** Each
  full sync reads all ~5.9M rows; do it ~25× over a month and you get
  ~150M. This is the App-B local-replica bootstrap / incremental resync —
  *not* a request path, and *not* something the prefix-index work touches.
  It's acceptable as a batch cost, but it's the reason "syncs" and "rows
  read" both spike together. If it ever creeps into something triggered
  per-request, this is where we'd catch it.
- **The 74.6M stats aggregate is a full scan on a hot path.** A
  `COUNT(*) + SUM(CASE…)` over `actors` reads the whole table every time
  `materializeStats` runs. That's textbook corpus-proportional work on a
  cron path. The fix is the `actor_deltas` rollup table (already in the
  schema) — increment counters on write, never scan. Worth confirming the
  full-scan version is actually retired; 74.6M says it isn't, or wasn't
  recently.
- **The 18.6M freshness probes are index-bypass waste.** Three handles
  should be three index seeks (~3 rows read), not 18.6M. The cause is the
  `actors.handle` index being `COLLATE NOCASE`; an ad-hoc
  `WHERE handle = '…'` / `handle IN (…)` without a matching `COLLATE
  NOCASE` can't use it and falls to a full scan. These are our own manual
  `turso db shell` checks. Cheap to fix: append `COLLATE NOCASE` to
  interactive handle lookups. (See project memory: *actors.handle index
  is COLLATE NOCASE*.)

### how to use it going forward

Run `turso db inspect typeahead --queries` periodically (start/end of a
billing window, or after shipping a path that touches `actors`) and ask:

1. **Any new query with rows-read in the tens-of-millions?** That's a
   corpus-scan that slipped in. Find it, decide if it belongs on a batch
   path or needs an index / precomputed structure.
2. **Did a known full-scan's number drop after we shipped a fix?** This is
   how we *verify* the stats-rollup migration actually killed the
   `COUNT(*)` scan — the number should fall toward zero next period.
3. **Is `rows written` climbing faster than ingest volume?** Points at a
   write-amplifying upsert or an over-eager `last_activity_at` churn.

It's a lagging signal — it tells you what already happened, after the
meter ran. But it's the cheapest possible audit of "is the system still
shaped the way we think it is," and it needs zero instrumentation in our
code. Pair it with the prefix-index work: once serving is bounded, the
*only* corpus-proportional reads left in this table should be batch jobs
(sync, snapshots), and `--queries` is how we'll prove that.

## someday: a skill

This is a good candidate for a `/turso-audit` skill — run the three
commands, diff `--queries` against the last snapshot, and flag any query
whose rows-read crossed a threshold or grew vs. the prior period. Not yet;
documenting the manual procedure first.

## reference

- `turso db inspect --help` (aliases: `usage`); flags `--verbose`,
  `--queries`
- `turso plan show`
- numbers are period-to-date, reset monthly (see `plan show` footer)
- related: `docs/turso-research.md` (capabilities / export wire format),
  project memory *actors.handle index is COLLATE NOCASE*

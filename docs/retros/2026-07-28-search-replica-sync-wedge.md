# Retrospective: 2026-07-28 → 07-31 search replica sync wedge

## What happened

The Turso→replica sync loop in the search service stopped making progress at
**2026-07-28 20:14 UTC** and stayed stopped for **48.7 hours**. Everything a
user asked us to index in that window was written to Turso correctly and never
became searchable. It was found because a user (`catbus.run`, a `did:web`
account on a self-hosted PDS) submitted their handle for indexing, it did not
appear, and they asked how long it was supposed to take.

The wedge was silent by construction: `syncLoop` catches every error and
continues, so a hang *inside* `incrementalSync` is indistinguishable from a
healthy idle loop. It never crashed, never logged, and `/health` reported
`ready: true` throughout while `sync_lag_s` climbed to 175,469.

Nothing alerted, for two independent reasons. The only alert in the project
watched the *ingester*. And `typeahead-search` had no `LOGFIRE_TOKEN` at all —
it had never exported a single span, so there was nothing to alert on even if a
rule had existed. The number was sitting in `/health` the entire time, correct
and machine-readable, and nothing read it.

Three unrelated silent failures were found while fixing this one. That is the
theme worth carrying: this system fails by going quiet, not by falling over.

## Timeline

| Time (UTC) | Event |
|---|---|
| 2026-07-28 09:03 | Snapshot `b1785229391-21fc` built and promoted. Normal. |
| 2026-07-28 20:14 | Last successful incremental sync. The loop wedges — most likely inside a Turso read with no timeout. No error, no log line, no crash. |
| 2026-07-28 20:14 → 07-31 02:00 | Search serves the 07-28 snapshot plus a frozen overlay. Ingest is healthy the whole time; Turso keeps receiving writes; the serving path never sees them. The 3-day snapshot rebuild is the only compensating mechanism, so effective freshness ceiling is ~72h. |
| 2026-07-30 20:51 | `did:web:catbus.run` submitted via `/request-indexing`. Row written to Turso correctly in ~1s. Not searchable. |
| 2026-07-31 ~01:30 | User asks how long indexing takes. `/health` shows `sync_lag_s: 175469`. |
| 2026-07-31 02:09 | `fly machine restart` on `typeahead-search`. `ready: true` immediately — search never 503s, because the worker falls back to Turso and the attached snapshot keeps serving. Full resync begins. |
| 2026-07-31 03:25 | Resync converges. `actors` 9,340,153 → 10,685,384, lag 186,707s → 135s. |
| 2026-07-31 00:05 → 01:34 | Off-cycle snapshot rebuild runs on heavypad. 12.4GB `index.db` uploads successfully; the 655-byte `manifest.json` 403s. The failure is swallowed and the flow reports `Completed()`. |
| 2026-07-31 02:35 | Artifact verified intact in R2 (`12398362624` bytes matching the manifest's own `byte_size`), manifest and `latest.json` published from App B, whose token still worked. Promote fires 16s later. |
| 2026-07-31 05:00+ | `/request-indexing` measured end-to-end against accounts confirmed absent: 1.3–6.4s, searchable ~0.16s after the write returns. |

## Root causes

### 1. A supervised-looking loop that nothing supervised

`syncLoop` is `while (true) { sleep; incrementalSync() catch log; }`. Every
failure mode it anticipates is an *error*. The one it hit was a *hang*, and the
shape of the loop makes a hang look exactly like an idle tick.

The self-heal made it worse in a specific way: `incrementalSync` contains a
"lag exceeds `SYNC_MAX_LAG_S` → bulk resync" branch, so on paper the system
recovers from precisely this state after 6 hours. It never fired, because **the
loop that evaluates the self-heal is the loop that was stuck.** A recovery path
that lives inside the thing it recovers is not a recovery path.

**Fix:** a liveness watchdog that exits on stall so fly restarts the process —
the same remedy the ingester got after its 13-day TLS wedge (`2026-06-12`
retro). Fed by any forward progress, so a multi-hour bulk resync cannot trip it.

### 2. Component liveness checks, no outcome check

Every check that existed was green: ingester heartbeat, promote poller build id,
actor counts, `/health` `ready`. All true, all useless — none of them measured
*whether a write becomes searchable*, which is the only property users have.

**Fix:** `/health/freshness` reports replica lag, snapshot age, and a live query
through the real serving path, and returns 503 when any of it is wrong. It is
public and needs no token, so an uptime check can watch it without depending on
the telemetry export that turned out to be missing.

### 3. Telemetry that was never wired up

`typeahead-search` had no `LOGFIRE_TOKEN`. The code was instrumented —
`logfire.span("sync.incremental")`, a `sync_lag_s` attribute on `/health` — and
none of it went anywhere. Confirmed by querying Logfire: zero records from any
service other than `typeahead-ingester`, ever.

**Fix:** token set (piped machine-to-machine, never through a transcript), plus
a `search replica sync silent` alert on the absence of `sync.*` spans for 30
minutes, mirroring the ingester heartbeat alert.

### 4. The fast path ran through the slowest component

`/request-indexing` wrote to Turso and returned "indexed" — true of the
database, false of the search the user was about to try. The first fix added
`/admin/refresh`, which took a DID and had the search service **read the row
back out of Turso**. Measured 2026-07-31 with a bulk job running: an indexed
point lookup took 28s, and the endpoint simply hung. The worker already had
every field; there was nothing to look up.

**Fix:** the worker sends the row. Nothing in the fast path touches Turso, and
the apply is a flat ~0.16s. Accepting caller-supplied values grants no new
privilege — the same `ADMIN_SECRET` already authorises `/admin/ingest` to write
arbitrary actor rows into Turso.

## Other silent failures found along the way

- **The indexer swallowed publish failures.** `uploadArtifacts(...) catch |err|
  log.err(...)` then fell through, so the process exited 0 and Prefect recorded
  `Completed()` for a build that never promoted. Now propagates, and
  `INDEX_PUBLISH_ONLY=<build_dir>` republishes a retained build without
  rebuilding — the failure comment always promised "a retry/manual upload can
  still promote it" and there was no such path.
- **heavypad's R2 token died mid-run.** It authorised a 46-minute multipart
  upload, then returned 403 on `PutObject`, `HeadObject` and `ListObjectsV2`
  from 01:34:57 onward. App B's token still worked, which is how the build was
  recovered without re-sending 12.4GB. Cause not determined — the API token
  available to us could not enumerate account tokens. **Still open.**
- **`/stats` cache key discarded the query string**, so every shared comparison
  URL collapsed onto one cache entry.
- **`seriesTip` was never declared** on `/stats`, so the chart tooltip threw
  `ReferenceError` on every hover and had never worked.
- **R2 holds 52 objects / 243GB** of unpruned builds against a `COSTS.md`
  budget of "~10GB + a few builds". Nothing deletes old builds. **Still open.**

## What went well

- The architecture insulated serving, exactly as designed. Search stayed up and
  fast for 48 hours on a stale snapshot; no user saw an error, only absence.
- The restart was safe and verified safe *before* it was performed: the worker's
  Turso fallback covers a 503ing backend, and lag was inside tombstone retention
  so the resync did not wipe.
- Publishing `latest.json` **last** meant a failed upload left the artifact
  unreferenced rather than promoting a pointer to an incomplete snapshot.

## What went badly

- **Diagnosis floundered.** Multiple rounds of asserting a fix worked based on
  the wrong signal: synthetic `click` events instead of touch, a cached PNG
  read back as "unchanged", and — worst — reporting `/request-indexing` as
  working when the result had actually come from the worker's Turso fallback,
  not the replica. The fallback masked the failure being measured.
- **A statistic was built on a bad sample.** "~1% of handles are stale, ~100k
  actors" came from one clustered sample (40 windows of 25 *consecutive*
  rowids, 7 mismatches). Consecutive rowids are correlated. A corpus-scale
  repair pipeline was designed and wired into a weekly job on that basis, then
  deleted. When 7 known-stale handles were re-checked hours later, 2 had already
  self-corrected — the population is not static and the relay repairs some of it
  on its own.
- **A corpus-scale job was started at the wrong time**, degrading Turso point
  lookups from 0.21s to 28s, which then corrupted latency measurements taken
  against the same database.
- **A watermark rollback nearly caused a livelock.** Rolling `last_sync` back 3
  hours to close an overlay gap, combined with the 20-minute watchdog added the
  same night, would have had each attempt killed before it could drain — and
  `last_sync` is only written after a full drain, so no attempt would ever
  finish. Caught before the deadline.

## Lessons

**A silent system is the failure mode here, not a crashing one.** Three of the
four root causes above are "it kept reporting success". Prefer a check that
measures the outcome users care about over any number of component-liveness
checks.

**A recovery path inside the thing it recovers is not a recovery path.** The 6h
self-heal was real code that would have worked, gated behind the loop that was
stuck.

**Measure the thing, not a proxy for the thing.** Every wrong conclusion in this
incident came from trusting a signal adjacent to the property being tested.

**Do not run corpus-scale work while measuring latency.** They interfere, and
the measurement is what tells you whether you are done.

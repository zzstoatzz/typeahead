# Bounded overlay lookup review — September 5, 2026

Decision: **local one-statement fix is tested; no deployment or ranking-index migration yet.**
The initial synthetic review made no production queries. The real-data follow-up
below used targeted read-only production exports; no production writes or deployments.

## What was tested

`services/src/index/index_search.zig` now includes 160 deterministic randomized
SQLite fixtures, four keys each, 240 possible DIDs per key. Both implementations
use the real SQLite lookup helpers and production merge. Cases include duplicate
DIDs across keys, ties, fractional f32 scores, empty base lists, overlay additions,
deletions, lowered/raised snapshot scores, union and intersection modes, and exact
keys bypassing the bound. The ordered DID/score lists matched in all fixtures.
Existing focused deletion/tie tests also pass. `zig build test --summary all`:
136/136 tests passed, 12/12 build steps succeeded.

This tests candidate ranking, not full serialized API responses against a corpus.
The unchanged exact promotion and hydration consume the same inputs on fixed
fixtures. Hidden/tombstoned actors are filtered AFTER the existing 50-candidate
cap: existing underfilled responses are not cured by this optimization. Requested
limits above 50 are already constrained by that cap (apart from exact reinjection).

## Why the bound can be exact

For a fixed state, union uses maximum score across keys, then score descending and
DID ascending. An omitted addition has 50 distinct additions ahead of it in its
own key. All those DIDs qualify for the union, with final scores at least that
key's score. Therefore the omitted candidate cannot enter the top 50. Cross-key
deduplication does not erase the 50 distinct DIDs within that key.

Every change to a base candidate must be fetched, even a deletion or demotion
below the cutoff, to prevent stale snapshot scores from surviving. Exact keys
remain full so low-score exact handles can be reinjected; intersections remain
full because a low-ranked per-token candidate may be the only common match.

The SQL and merge tie ordering must agree. The normal writer stores widened f32
scores, and DID ordering is binary. Changes to scoring, sum aggregation for unions,
HYDRATE_CAP, or score storage precision require rechecking the proof.

## Confirmed race

ReadLease holds a connection-slot mutex, not a SQLite read transaction. The new
helper runs top-50 SELECT, then a SELECT for base corrections. If a writer lowers
a base candidate's score between them, the helper includes its new low score but
never fetches the newly eligible 51st candidate. It produces neither the original
ranking nor the new one. Reproduction: `python3 scripts/test_bounded_lookup_race.py`.

Remedy to evaluate: a single SQL statement combining the bounded additions and
base corrections, or an explicit read snapshot. A transaction across the entire
search would also address existing cross-key/hydration consistency questions, but
its effects on WAL/checkpoint behavior should be measured rather than assumed.

## Performance qualification

The schema has PRIMARY KEY(key,did), not a (key,score) ranking index. SQLite still
visits matching prefix rows and uses a temporary ordering structure. LIMIT bounds
rows transferred into Zig and merge allocations; it does not establish constant
SQLite work. A ranking index adds write, storage and compaction costs.

## Release gate

1. Remove the two-statement race and retain a regression test for the repaired path.
2. Add full-pipeline differential cases for exact handles, multi-word/CJK queries,
   moderation, missing hydration blobs, and requested result limits.
3. Compare ordered responses on a fixed offline snapshot/overlay with representative
   and adversarial queries. Require zero unexpected membership/order differences.
4. Benchmark hot prefixes and ordinary queries: SQLite time, total latency, rows
   materialized and memory. Avoid production load generation.

Human relevance judgments are useful for changing ranking policy. For this proposed
optimization, exact differential equality is the stronger first gate: no planned
quality tradeoff should be accepted or disguised as improved performance.

## Hardening follow-up

The bounded helper now uses one compound SELECT (top additions UNION ALL base
corrections). Both branches share a statement snapshot; duplicate rows agree and
are harmless to the merge map. There is no new long-lived read transaction.
The existing broader cross-key/hydration consistency question remains unchanged.

A two-connection, file-backed WAL regression commits a score change after the
reader begins stepping. Both branches see the old score; a subsequent statement
sees the new score. The earlier failing two-statement model remains alongside it
as a documented counterexample. Run `python3 scripts/test_bounded_lookup_race.py`.

A test-only unbounded instantiation of the real execute pipeline now permits
comparisons through query-key selection, merge, exact promotion and hydration.
Fixtures cover short prefixes, exact handles, multi-word queries, CJK, accents,
empty results, hidden blobs, hidden overlay actors, tombstones and absent actors.
Ordered DID, handle, display name and score match. These are synthetic fixtures,
not a representative production-corpus evaluation or serialized HTTP comparison.

### Query-plan experiment

Run `python3 scripts/bench_overlay_plan.py`; saved output is
`bounded-overlay-plan.json`. Python SQLite 3.53.0, synthetic 333,000-row prefix,
seven warm query runs; output transfer is included. This is not production Zig
latency. The 2,000-update measurements are single batches, not statistical estimates.

| Query | Median ms | Rows returned |
| --- | ---: | ---: |
| Full | 83.015 | 333,000 |
| Bounded compound SELECT | 14.549 | 350 |
| Bounded + experimental partial ranking index | 0.171 | 350 |

Without the ranking index, SQLite searches by key and uses a temporary B-tree
for ordering. Base corrections use primary-key point lookups. The candidate
`(key, score DESC, did ASC) WHERE op=1` index produces a covering lookup without
the temporary sort. In this fixture it adds 7,725,056 bytes, takes 101 ms to
build, and changes a 2,000-row score-update batch from 2.67 to 15.85 ms. Real
prefix fanout, WAL traffic, cache pressure, and compaction need measurement before
adopting it. No production schema changes were made.

Within-reason stopping point: retain the race fix and regression coverage; defer
index adoption and whole-search transactions. A representative offline corpus
comparison and bundled-SQLite performance run remain release gates.

Sources read:
- https://www.sqlite.org/isolation.html
- https://www.sqlite.org/wal.html
- https://www.sqlite.org/queryplanner.html
- https://www.sqlite.org/optoverview.html

## Parallel index experiment on heavypad

User authorized pursuing the ranking index alongside the bounded canary if
resource/cost growth stays modest. Heavypad checked quiet, ~59 GiB available RAM
and 931 GiB free disk. Initial synthetic experiment ran with nice 15, a 120-second
timeout, and a temporary database deleted on completion. No provider resources,
Turso calls, production shadow traffic or additional monitoring were created.

Linux system SQLite 3.37.2: full 105.08 ms; bounded 19.433 ms; indexed 0.233 ms.
2,000 updates: 7.238 ms without index vs 18.879 ms with index; index 7,725,056 bytes.
Saved `bounded-overlay-heavypad.json`. Same synthetic fixture, not bundled SQLite
or a real corpus, and not a running canary. Latest existing home snapshot build
b1788512492-0ed0 is available under /home/stoat/typeahead-index/build.

Next isolated stage should share one immutable snapshot and use separate scratch
overlays for baseline/indexed variants. Do not point it at the builder's writable
files. Bound resource use before scaling the fixture; reuse captured updates for
both variants rather than starting two Turso pollers. Measure equality, apply
latency, WAL growth, index size and compaction alongside query latency. Live Fly
canary provisioning remains outstanding; do not imply it is serving shadow traffic.

## Real-data evaluation — September 5 local / September 6 UTC

**Pass for the captured cases, with and without the experimental index.** The
opt-in Zig differential test calls both actual lookup implementations, including
merge, exact promotion and hydration. It compares every serialized public actor
field in order, plus internal scores. All 12 query cases match, eight repetitions
each per index configuration. Both runs pass all 139 tests.

Data: 350,163 real `prefix_overlay` rows captured by indexed key lookups from Fly,
including 348,716 `d:bs` rows. The matching 14 GB immutable snapshot
`b1788512492-0ed0` was already on heavypad; no Turso export or snapshot build was
needed. Hydration candidates were selected from the full merged candidate sets;
324 DIDs were checked by indexed actor-overlay reads. Missing overlay rows fall
through to the real snapshot. Scratch artifacts are in
`/home/stoat/typeahead-eval-38`, separate from the builder and serving databases.

Queries: `bs`, `nate`, `nowack`, `alice`, `pds.dad`, `zzstoatzz.io`, `日本`,
`unlikelymissing38`, `nate nowack`, `bs bs`, `nate nate`, and `ALICE`. The repeated
word cases exercise non-empty intersections; `nate nowack` is empty in this data.
These are hand-selected probes, not a traffic-weighted query sample.

Warm medians of seven runs after one warm-up, heavypad, Zig ReleaseFast, bundled
SQLite, test allocator, complete lookup/merge/hydration but excluding HTTP:

| Query | Full lookup | Bounded lookup | Bounded + ranking index |
| --- | ---: | ---: | ---: |
| `bs` | 1,035.1 ms | 19.975 ms | 1.250 ms |
| `nate` | 0.215 ms | 0.438 ms | 0.403 ms |
| `alice` | 0.257 ms | 0.481 ms | 0.414 ms |
| `pds.dad` | 0.124 ms | 0.147 ms | 0.133 ms |
| `日本` | 0.166 ms | 0.259 ms | 0.234 ms |

Small queries incur extra SQL overhead, measured in fractions of a millisecond.
Intersections and exact keys remain unbounded by design. The large-prefix gain
is substantial, but these timings are not production latency predictions: host,
allocator, data slice, cache state and concurrency differ. Full always precedes
bounded within each iteration. See [raw measurements](bounded-overlay-real.json).

The scratch-only partial index is `(key, score DESC, did ASC) WHERE op=1`.
An additional Python/SQLite 3.37.2 query-plan experiment on the same real rows
changes `SEARCH PRIMARY KEY` plus `USE TEMP B-TREE FOR ORDER BY` into
`SEARCH ... USING INDEX eval_rank`, with no temporary sort. Top-50 SQL alone
falls from 26.636 ms to 0.033 ms. This is a different harness from the Zig table.

Measured index creation: 184 ms, 15,966,208 bytes added (15.2 MiB). Seven
2,000-row score-update batches, each rolled back, have median execution times
2.895 ms without the index and 23.630 ms with it. Commit/fsync is excluded;
these are existing-row score changes, not a replay of inserts, deletes, or full
actor-to-prefix fanout. This is evidence of write amplification, not a full
sync/build cost estimate. Reusable scratch-only helper:
`scripts/bench_real_overlay_index.py` (it creates the index in its explicit target).

Limitations: this is a targeted overlay slice, not the entire 5.2 GB local DB;
prefix and actor exports happened at separate instants, then all variants read
one frozen assembled fixture. It is not an atomic historical production replay.
The real cases supplement, rather than replace, the randomized moderation,
tombstone, demotion, tie, and concurrency fixtures above. No claim of ranking
quality improvement: this is an equivalence/performance optimization.

Production stayed healthy after capture: HTTP P95 23.8 ms, 277 HTTP 200s and no
other statuses in the checked five-minute window, zero CPU throttling, freshness
probe passing and no ingester backpressure. No production schema or code changed.

Next release gate: establish full-overlay index size and paced sync/fanout cost
on an isolated copy, then a bounded canary with correctness/latency/resource
comparison and rollback. Do not build the index synchronously on the serving
connection based on the small-slice timing. The present evaluation justifies
that next step; it does not mark the ranking index deployed.

To repeat the pipeline check on an isolated fixture (both files required):

```sh
cd services
TYPEAHEAD_EVAL_OVERLAY=/absolute/scratch/overlay.db \
TYPEAHEAD_EVAL_SNAPSHOT=/absolute/snapshot/index.db \
zig build test -Doptimize=ReleaseFast --summary all --seed 386
```

The opt-in test opens the overlay read-only and sets `query_only`; it never starts
the server, sync loop or builder. With no explicit paths it skips. Use a different
build seed when repeating against externally changed fixture contents so the
build system reruns the tests. Plain filesystem paths are supported; this bundled
SQLite build did not open the snapshot with the attempted `file:...?mode=ro` URI.

### Production build-mode follow-up

The same captured slice also passes 139/139 tests under Zig **ReleaseSafe**, the
mode used by the production Dockerfile. Its indexed run measures `bs` full lookup
1166.811 ms versus bounded lookup 1.475 ms; all 12 ordered public-response
comparisons pass. The full-data canary preparation is tracked in
[the canary runbook](../../ops/canary/README.md); the restored evaluation machine
has no traffic routing and is not a deployed search canary.

## Full restored overlay — September 6, 2026 UTC

The isolated Fly machine (4 shared CPUs, 2 GiB RAM, same allocation as production)
restored an existing volume snapshot. SQLite `quick_check` passes. The full overlay
has **11,158,428 prefix rows and 251,841 actor rows**, watermark 1788585049, and
matches snapshot build `b1788512492-0ed0`. This is an older, internally coherent
fixture than the targeted capture; its `bs` row counts therefore differ.

The partial ranking index occupies **569,638,912 bytes (543.25 MiB)** and built in
**97.663 seconds**. It reused free pages: the 5,510,037,504-byte DB file did not
grow. Freelist pages fell from 971,135 to 832,063; sizing by file growth alone
would incorrectly call this index free. The top-50 plan uses `canary_rank` and
has no temporary sort. The index and write benchmarks used disposable copies. After those gates
passed, the ranking index was also added to the restored source for the private
shadow pilot; its actor/prefix data remains unchanged.

**36 hand-selected queries pass the ordered JSON/score comparison on both copies**
(72 comparisons total), including single-character prefixes, handles, names,
non-empty multiword matches, accents, case/whitespace, CJK, empty and punctuation.
The standalone comparator runs under a 1.5 GiB address-space cap, 10-second CPU
limit and 15-second wall timeout. Peak child RSS stays below 124 MiB. It uses one
read transaction per comparison. These are parity checks, not traffic-weighted
ranking-quality evals or unbiased latency benchmarks: baseline runs first in
each new process and warms pages for the candidate.

For `bs`, 237,332 overlay rows became 252 fetched rows. On the plain copy the
baseline/candidate measured 756.4/59.3 ms; on the indexed copy 695.1/3.5 ms. These
single-run figures illustrate the change; they do not establish production P95.
Raw cases: [plain](full-overlay-plain.json), [indexed](full-overlay-ranked.json).

The actual `projectUpdate` / `projectDelete` functions then processed five
**committed 500-actor batches**, using real packed profiles and controlled changes.
SQLite settings match production (WAL, synchronous NORMAL, 20 MB cache, 1,000-page
autocheckpoint). Both configurations started from identical data.

| 500-actor batch | Plain | Indexed |
| --- | ---: | ---: |
| Unchanged profile | 2.920 s | 4.491 s |
| Score change | 3.657 s | 6.903 s |
| Rename | 3.677 s | 5.364 s |
| Insert | 2.548 s | 7.387 s |
| Delete | 3.684 s | 5.154 s |

Whole-process elapsed time: **18.45 vs 31.38 seconds** (includes unit-test setup).
Observed peak WAL, sampled at 50 ms: **43,972,792 vs 122,516,472 bytes**; this is a
lower bound on peak WAL size, not total bytes written. Child RSS: about **48 MiB**
in both runs. There was one batch per scenario, so do not claim stable medians.
This includes prefix derivation, actor packing, SQL projection and commit, but
excludes Turso fetch and old-row lookup. It is not a full production sync replay.

The additional storage fits existing disk space. The measured write overhead
supports a limited shadow canary; it does not justify increasing bulk enrichment
or running index creation on production's serving connection. Full measurements
are in [the summary](full-overlay-summary.json). Concurrent-write parity and
exposure/rollback gates remain tracked in [the runbook](../../ops/canary/README.md).

### Concurrent-update gate

A writer replayed committed actor updates while the comparator ran 108 queries
against the same mutable indexed copy. The first attempt had 21 subprocess
failures before JSON output; it did not emit an unequal result. That attempt's
stderr was not retained, so its exact error cannot be proven retroactively.
The diagnostic rerun passed all 108.

A separate controlled 150 ms exclusive lock reproduced `error: Busy` with the
initial comparator. It lacked `LocalDb.applyReadPragmas`' one-second busy timeout.
The comparator now sets `query_only` and `busy_timeout=1000` before ATTACH,
matching production's read pool. Tests using the real executable verify both
brief-lock success and persistent-lock bounded failure (`test_lookup_locks.py`).
The same lock probe passes on the isolated Fly machine, and a fresh full-data
concurrent run passes **108/108**, writer exit 0, peak child RSS 125,268 KiB.
This closes the tested lock-handling gap without claiming the first failures
were definitively diagnosed. Production code's read-pool settings were unchanged.

### Initial private shadow pilot

On September 6, the isolated Fly comparator received live query samples through
a time-limited Wrangler tail session and private proxy. All 18 admissions in
ten minutes matched the entire ordered candidate JSON and scores. Two earlier
live samples also matched; a separate `bs` smoke check brings server totals to
21/21. The pilot auto-paused on exit and the machine was stopped.

No public response used the candidate. Queries were replayed on the same frozen
fixture for both implementations; this does not test live freshness or sync
catch-up. Samples include edge-cache hits, and baseline-first timing is biased
by page warming. See [raw metrics](live-shadow-pilot.json) and the
[runbook](../../ops/canary/README.md) for limits, rollback and remaining gates.

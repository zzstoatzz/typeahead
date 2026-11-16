# Statistics must not scan serving data

Assessment requested November 16, 2026, after the standby experiment began.
The replacement is deployed to both slots; see verified status below.
The paced reader is an experiment, not the architectural fix.

## What the history says

| Commit | Original decision or regression |
| --- | --- |
| `ef6c347` (June 8) | Replaced a roughly 300-second corpus count that froze statistics with a maintained actor counter. |
| `4b96dd3` (July 30) | Fixed an apparently selective stats query that actually scanned 10.6 million entries and timed out during write contention. |
| `efd3ac7` (August 16) | Removed corpus-scale cron selectors and added query-plan deploy guards. The contract was bounded work, not a different scheduler. |
| `de89cda`, `6c8cf08` (November 10) | Reintroduced a full corpus read for counts in the new store, then ran it hourly on a separate thread and read handle. That separated locks, not disk, cache or CPU resources. |
| `7de6aa3`, `444f3fa` (November 11) | Removed counts from open and delayed scans behind startup. This reduced startup interference without removing recurring observational scans. |
| `9161721`, `1385496` (November 12) | Added index-side count queries and exposed the store scanner's last report through `/corpus`. |
| `87abcae`, `ee1600f` (November 14) | Removed old Turso count paths and their maintained counter writer after switching statistics to slot reads. The old bounded-work property did not survive the migration. |

The failure is recurring in different storage implementations. Neither moving
the trigger to a cron/Worker nor giving the reader another thread makes it safe.

## Required changes

### Maintain facts with the existing writes

The assessment initially proposed store counters. Source inspection found a
simpler owner: Tantivy already maintains actor counts transactionally, and every
indexed actor mutation passes through its commit. Extend that metadata rather
than introducing a second maintained counter system in ZKV. Update each actor's old/new
contribution in the same index commit as its mutation. Replacement, replay,
abort and restart preserve the counters. Tombstones contribute zero, so pruning
them changes no live statistic.

Maintain index-side exclusion counters alongside the existing metadata updates
for actor and searchable totals. Do not replace `Count` with another query over
the same postings. Existing metadata without new counts is explicitly unknown,
not a reason for `/corpus` to enumerate documents or postings.

Queue lengths remain explicitly unknown rather than adding a second transaction
system to track them. Stop enumerating queues just to report health. Time-dependent due counts may be
unknown or a small explicitly bounded sample; backlog/admission decisions need
their own bounded probes. Use the storage engine's maintained byte statistics
and bounded filesystem metadata calls instead of enumerating every file for a
health report. Necessary ingestion/moderation maintenance is separate from
observational statistics and retains its existing work budgets.

### Remove recount capability from serving roles

Remove `Scanner.run` and the hourly scan configuration from the production
store role. Keep recount code in an explicit offline CLI, separate from the normal serve
command. The existing image already carries offline import commands; reuse that
maintenance boundary rather than adding another executable/build/deployment.
The checked helper refuses the serving origin and quiesces the standby, and
the index writer lock prevents initialization alongside a serving process. `/corpus`, `/health`, startup, cron and metrics
export may only read maintained facts or previously saved snapshots. No
missing-value, recovery or debugging path may fall back to a corpus read.

Stats corruption or a missing initialization marker invalidates the statistics;
it must not cause a recount, prevent actor processing or fail a search. Retain
the last published value with its actual timestamp, or report unknown. A new
counter format needs a revision/provenance marker so an older writer cannot
silently leave apparently current counters behind after rollback.

### Isolate initialization and audits

Seed counters once on a standby fenced out of routing, with its writer
quiesced, or from an isolated cloud snapshot plus an exact delta replay.
Never initialize by scanning during normal startup. Prefer separate cloud
copies for subsequent full audits, with their own disk, memory and CPU. A
separate process on a serving volume is insufficient isolation.

Admission must reject an audit/bootstrap workload and require the serving
binary's bounded-statistics capability. A route flip must not promote a slot
whose audit is still active. Bootstrap/recount tooling must refuse a serving
target. Existing normalizer, format, PDS, policy and freshness admission gates
remain required.

### Keep collection and pages outside the computation

Keep the existing statistics persistence in the Worker, D1 and KV. It reads
small, already-maintained facts; it never tells a slot to compute them. Failures
leave stale/missing stats and cannot hold up search.

The eval is already a separate Cloudflare Worker with a Durable Object for
coordination and quotas. It publishes immutable reports and their index to KV;
the public Worker renders `/eval` from those snapshots. No page visit triggers
collection. That architecture can remain. External collection still consumes
the target service's request budget, so isolation of the runner does not grant
permission for load tests against a serving target.

## Enforcement and acceptance gates

| Boundary | Required check |
| --- | --- |
| Production build | The store role cannot import a scanner; the index server cannot invoke the offline initializer. Scan-enabling configuration is rejected, not silently accepted. |
| Counter transitions | Independent index and bridge models cover replacement, replay, rejection, hidden/deleted/restored actors, crash/reopen and reconciliation. Tombstone pruning contributes zero. |
| Read cost | Metadata-only `/corpus` and health snapshots; structural regression guards reject scanner imports, count queries, queue enumeration and filesystem walks. Missing/invalid counters perform no scan. |
| Index counts | Differential tests cover live/deleted/replaced documents and changed moderation without posting-list enumeration on reads. |
| Isolation | Hung/unavailable collectors, D1, KV and telemetry do not delay search; missing stats remain unknown. |
| Admission | Reject active audits, uninitialized required capabilities and old binaries that can silently resume observational scans; include an unavailable serving slot in recovery tests. |
| Performance | Compare an explicit isolated target before/after under live updates, preserve search results, and measure tails as well as counter-write cost. |
| Deployment | Publish, initialize/qualify standby, verify admission and correctness, then perform an explicitly authorized route flip and upgrade the other slot. |

The categorical guarantee is that statistics cannot initiate corpus-size work
or share a full audit's resource demand with serving. Merely reducing scan
priority, scan frequency or per-page speed does not satisfy these gates.

## Verified rollout

Verified 2026-11-16 at 22:26 UTC. Slot B serves; A is a fit standby. Both run
image `sha256:b015bfaed4c025d94e7c3159152872ad44452367cdeb6b6e61acaf3acf1adff5`
and index binary `47080fed8fb8c252a6b78007fdcc07e11847bbed55181645ef87651192a0b531`.
The public Worker is `28abe765-0b0d-4e30-ab8b-d91a8d3e29d4`. The implementation
was published as `8354e5a` before deployment. The helper restoration fix is
`2ffebf1`; `69f2772` adds the bridge counter/recovery checks.

Both stores report `statistics_version: 1`, no corpus report, no observational
queue/filesystem totals, zero pending writes and zero bridge lag at the recorded
health samples. Both indexes have initialized counters that continue advancing
with live commits. The normalizer, plan, index format, machine sizes and storage
roles are unchanged. B still carries the harmless retired scan-disable setting;
A has no such override, and the scanner is absent from both serving roles.

The [machine-readable rollout record](2026-11-16-statistics-isolation-rollout.json)
retains identities, admission and refusal evidence, counts, query baselines and
all qualification attempts. No new infrastructure or schedule was introduced.
The existing Worker saves snapshots in D1/KV; the isolated eval runner and its
saved reports are unchanged. `/stats`, `/eval` and saved eval data returned 200.
Historical snapshots remain intact. New counts describe indexed live actors;
the previous store scan also included roughly 850 live actors without handles.

### Rollout and qualification

The compatible Worker deployed first and passed smoke/image checks. B then
received the image and one offline initialization. The first helper version
incorrectly assumed Fly would clear an init override when given an empty object.
Initialization succeeded, but restoration left B asleep. The route stayed on A.
B was restored with an explicit `/app/supervise` command; every other machine
setting matched. The corrected helper restores that command explicitly, tests
Fly's merge behavior and failure recovery, and completed A's initialization and
configuration verification without intervention.

Initial post-restart probes are retained: B's first 36 searches had four
timeouts; a second attempt had no failures but a 594 ms p90. A's first attempt
had seven timeouts. Neither cold nor repeated warm samples were used to waive
the latency gate. B was promoted only after catch-up drained, I/O settled, and
a fresh round after idle passed. A remained on standby through its warm-up.

| Settled slot | Searches | Successful / same ordered results | Engine p50 | Engine p90 | Maximum | Interleaved stats reads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| B | 36 | 36 / 36 | 1.088 ms | 1.257 ms | 1.876 ms | 12, all successful |
| A | 36 | 36 / 36 | 1.690 ms | 4.983 ms | 7.560 ms | 12, all successful |

These are qualification measurements over 12 retained queries on the real
corpus, not capacity estimates or an unbiased production latency distribution.
B's earlier baseline already had its scanner disabled, so these before/after
numbers are not a causal estimate of the scanner removal's speedup. The
[isolated scan experiment](2026-11-16-standby-scan.md) establishes that mechanism.
Startup warming and ingestion maintenance still cost resources; predictable
statistics do not eliminate those costs.

The live old-binary admission was refused. Offline B could not pass admission,
and the initializer refused serving A before issuing a machine mutation. The
route was changed only through the conditional route tool. Public smoke checks
passed after the flip. Ranking checks passed all structural criteria and retained
one existing parity miss: Bluesky's `fightinglabs.bsky.social` result for `fig`
is absent from our top eight. There was no retrieval change or result-order
regression in the retained query cases. Once A was upgraded, its fresh admission
was checked; recovery should use a fresh cutover to A, since the previous route
record names its old binary.

### Regression evidence

- `just check`: typecheck/lint, 248 Bun tests, 59 routing tests, three helper
  tests, 14 supervisor tests, five smoke tests and four deployment builds.
- `just test-services`: 334 tests, including kill/reopen and bounded
  control/enrichment backlog behavior.
- `just --no-dotenv test-tantivy`: 24 library tests plus normalization,
  HTTP, reconciliation, policy, PDS, crash/retry and merge checks.
- `just --no-dotenv test-bridge`: 141 tests; corpus counters match the source
  model through generated mutations, bridge kills and real server restarts.
- Counter history seed `20261116`: 160 actor replacements plus replay,
  deletion/reactivation, avatars, hidden flags and excluded handles; four
  reopens and an offline recount match an independent map oracle. Missing and
  malformed counts remain unknown while actor changes continue.
- Disposable planted avatar-count and deletion-count bugs both fail the history
  oracle. The HTTP check refuses initialization beside a running index writer.

CI caught a stale integration fixture calling the removed reporting endpoint;
it was replaced with the stronger counter oracle. CI also caught the timing
fixture crossing a rate-limit window at 22:05:59.957–22:06:00.135 UTC. Its limiter
is now deterministic; production rate limiting is unchanged. Both fixes passed
their local suites before publication. Worker, route-tool and index/bridge CI
all passed on code commit `69f2772` (pipeline `3n2hkv4p5xa22`).

No trial servers or corpus copies remain. Temporary terminals were closed after
logs were retained. These are bounded checks, not a general guarantee against
all I/O contention: request counters still write asynchronously to D1, and
normal ingestion, search warming and maintained storage metadata retain their
existing resource costs.

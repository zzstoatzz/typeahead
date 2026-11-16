# Statistics must not scan serving data

Assessment requested November 16, 2026, after the standby experiment began.
Implementation and rollout are in progress; see current status below.
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
| Store transitions | Replay generated histories against an independent count model, covering abort, crash/reopen, duplicate events, multi-update batches, moderation, disappearance/reactivation and pruning. |
| Read cost | Instrument storage reads: `/corpus` and health statistics perform a fixed amount of work as fixture size grows. Missing/invalid counters perform no scan. |
| Index counts | Differential tests cover live/deleted/replaced documents and changed moderation without posting-list enumeration on reads. |
| Isolation | Hung/unavailable collectors, D1, KV and telemetry do not delay search; missing stats remain unknown. |
| Admission | Reject active audits, uninitialized required capabilities and old binaries that can silently resume observational scans; include an unavailable serving slot in recovery tests. |
| Performance | Compare an explicit isolated target before/after under live updates, preserve search results, and measure tails as well as counter-write cost. |
| Deployment | Publish, initialize/qualify standby, verify admission and correctness, then perform an explicitly authorized route flip and upgrade the other slot. |

The categorical guarantee is that statistics cannot initiate corpus-size work
or share a full audit's resource demand with serving. Merely reducing scan
priority, scan frequency or per-page speed does not satisfy these gates.

## Current status

2026-11-16: implementation is locally qualified; rollout has not begun.
The serving slot A is unchanged. B still has the temporary scan-disable setting.

The index counters intentionally describe indexed live actors. The former store
scan included roughly 850 live actors without handles, which the bridge maps to
index tombstones. Historical snapshots remain intact; new snapshots will use the
indexed population. No search result or indexing eligibility changes.

Local evidence:

- `just check`: typecheck/lint, 248 Bun tests, 59 routing tests, two offline
  helper tests, supervisor/smoke tests and all four Worker deployment builds.
- `just test-services`: 334 tests, including real kill/reopen and bounded
  control/enrichment backlog behavior. Health does not enumerate queues/files.
- `just --no-dotenv test-tantivy`: 24 library tests, normalization/differential,
  HTTP, reconciliation, policy, PDS, crash/retry and merge checks.
- Counter history seed `20261116`: 160 actor replacements plus replay,
  deletion/reactivation, avatars, hidden flags and excluded handles; four
  reopens and an offline recount match an independent map oracle. Missing and
  malformed counts remain unknown while actor changes continue.
- Disposable planted avatar-count and deletion-count bugs both fail the history
  oracle. The HTTP check refuses initialization alongside a running index writer.
- Structural regression guards prohibit scanner imports/reporting in the store
  role, count queries on `/corpus`, and the old reporting endpoint.

These are bounded tests, not a general claim that observational I/O cannot ever
fail. Request traffic counters still write asynchronously to D1. Existing engine
metadata, cursors and ingestion maintenance retain their normal resource costs.
No new service, database or schedule was added. Production rollout and standby
latency evidence will be recorded here after verification.

# Statistics must not scan serving data

Assessment requested November 16, 2026, after the standby experiment began.
This is the required replacement design, not a claim that it is deployed.
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

Persist fixed-size corpus counters in the store, updating each actor's old/new
contribution in the same batch as its mutation. Cover discovery, profile and
identity changes, activity/ranking changes, enrichment, moderation overrides,
label expiry, deactivation/reactivation and tombstone removal. `Batch.save`
already centralizes actor writes; tombstone pruning is a separate deletion
path that must also participate. Multiple changes to a DID in one batch must
telescope correctly. Abort, replay, crash and restart must preserve the same
counts as the actor records.

Maintain index-side exclusion counters alongside the existing metadata updates
for actor and searchable totals. Do not replace `Count` with another query over
the same postings. Existing metadata without new counts is explicitly unknown,
not a reason for `/corpus` to enumerate documents or postings.

Track exact pending queue lengths when their keys are inserted/deleted. Stop
enumerating queues just to report health. Time-dependent due counts may be
unknown or a small explicitly bounded sample; backlog/admission decisions need
their own bounded probes. Use the storage engine's maintained byte statistics
and bounded filesystem metadata calls instead of enumerating every file for a
health report. Necessary ingestion/moderation maintenance is separate from
observational statistics and retains its existing work budgets.

### Remove recount capability from serving roles

Remove `Scanner.run` and the hourly scan configuration from the production
store role. Keep recount code in an explicit operator tool that is not copied
into the production image. `/corpus`, `/health`, startup, cron and metrics
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
| Production build | Normal store/index roles cannot import or invoke the recount tool; production image omits that executable. Scan-enabling configuration is rejected, not silently accepted. |
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

The serving slot has not been changed. Its existing hourly scanner remains a
known violation of the required design. The standby experiment measures the
cause and a mitigation; it does not establish that the production guarantee
exists. Its temporary configuration and processes must be retired as described
in [the experiment record](2026-11-16-standby-scan.md).

README describes the currently deployed architecture and is not rewritten to
claim this replacement has shipped. Update it and the operating/API documents
with the implementation and verified rollout, including unknown/stale counts.

# September 5: ingester CPU quota and misleading dashboard

Investigated September 5 using Fly Prometheus and Logfire records. No service restart, deployment, or machine resizing performed.

## Finding

The ingester exhausted its shared-CPU burst balance. The five-minute throttle rate exceeds 10% from 04:04 through 08:35 UTC September 5 (23:04 September 4 through 03:35 September 5 CDT), peaking at 68%. Burst balance declines from 500 CPU-seconds at 23:00 UTC September 4 to about four seconds at 04:00, then remains essentially zero until recovery. Machine uptime is continuous.

The dashboard's original `100 - idle` query wrongly presented steal time as application CPU execution. During throttling, steal alone was roughly 50–60%; actual user/kernel execution was about 6.7–7.4% during 05:00–08:00 UTC. Corrected the live panel to **CPU execution**, with:

```promql
avg by(app) (
  sum by(app,instance,cpu_id) (
    rate(fly_instance_cpu{
      app=~"typeahead-search|typeahead-ingester",
      mode=~"user|nice|system|irq|softirq"
    }[5m])
  )
)
```

Fly reports CPU counters in centiseconds, so the rate is already a percentage. Steal and idle accounting need not sum cleanly to 100 in these observations; summing execution modes avoids attributing scheduler waiting to work. The separate throttle query is `rate(fly_instance_cpu_throttle[5m])/100`, in seconds throttled per second, not request latency.

Fly documents a 6.25% sustained quota per shared vCPU, with up to 500 CPU-seconds of accumulated burst allowance: https://fly.io/docs/machines/cpu-performance/

## Ingestion impact

Logfire heartbeat totals rose from 1,673,261 to 2,016,618 within 04:05–08:35 UTC: 343,357 actor events, not unique accounts. There were 263 heartbeats. The warning/error query over 03:00–11:00 UTC found one failed delete batch at 07:28:14, no backlog overflow, and no firehose disconnect. Current logs at 19:24 UTC show successful five-second flushes with pending zero and an advancing relay cursor. Current identity queue is zero; that is a separate queue.

Higher earlier throughput, falling throughput overnight, and the final transition to small five-second batches are consistent with replay/catch-up after the September 3–4 outage. This is an inference: current heartbeat telemetry lacks the relay event timestamp needed to quantify historical ingestion lag or prove when it caught up. CPU quota exhaustion is directly supported by the balance and throttle series; the workload's precise origin is not.

## Separate earlier data loss

At 23:03:58 UTC September 4, a backlog overflow discarded 4,000 queued ingest events and one delete. At 23:25:32 UTC, another discarded 4,000 ingest events. Both followed repeated failed ingest POSTs; neither coincided with CPU quota exhaustion. Trace IDs:

- `01a06eaa206b35c6f555734a4fa1f4db`
- `01a06ebde132f150929f68c3bb0eb3d6`

These are queued events, not necessarily 8,000 distinct accounts. `services/src/ingest.zig` explicitly clears both queues and advances its cursor on overflow; restart alone cannot replay the discarded interval. The upstream reason for those write failures and recovery coverage remain unverified. Do not describe the full period as lossless based on search latency or current readiness.

## Follow-up

- Export source-event age, successful-write age, main-queue depth and discarded-event counts. Identity metrics do not cover this loss path.
- If catch-up time is unacceptable, evaluate more CPU quota using measured ingestion lag; the apparent 60% application CPU spike was not a reason to resize.
- Investigate the two earlier write-failure windows and recovery separately. Avoid replaying records into production without checking ordering/idempotency of profile updates and deletes.

## Expanded investigation: September 4 write failures

The two 23:00-hour overflows were only the tail of a larger incident. Querying
Logfire from September 4 00:00 through September 5 20:00 UTC returned 90 overflow
records, first at September 4 18:16:01.804764 and last at 23:25:32.850138 UTC.
Their logged queue sizes total **360,000 ingest entries and 114 delete entries**.
These are observed queue discards, not unique accounts or a count of permanently
missing database writes. No later overflow appears in this query. Telemetry is
not a durable event ledger, so this is not a proof of complete coverage.

| September 4 UTC hour | Logged overflows | Ingest entries | Delete entries |
| --- | ---: | ---: | ---: |
| 18 | 35 | 140,000 | 35 |
| 19 | 12 | 48,000 | 16 |
| 20 | 24 | 96,000 | 37 |
| 21 | 10 | 40,000 | 13 |
| 22 | 7 | 28,000 | 12 |
| 23 | 2 | 8,000 | 1 |

### What failed

Eleven recorded ingest HTTP 500 responses between 18:33 and 19:09 UTC contain
upstream database errors: eight `SQLITE_UNKNOWN: Database connections limit
exceeded, try to reduce concurrency` and three `SERVER_ERROR: Server returned
HTTP status 524`. This directly establishes database trouble in the earlier
part of the incident, consistent with the September 3–4 saturation retrospective.
It does not establish the cause of every later failed POST.

There are also 73 recorded identity POST `ResponseHeadTimeout` messages in
18:00–24:00 UTC. The main ingest POST path catches transport exceptions and
returns false without logging the error name. Consequently the later generic
`ingest batch failed` messages do not distinguish timeout, connection failure,
or another transport error. No Worker service records are present in the
Logfire project for the 23:00–23:30 interval queried; server-side attribution
for that interval remains unresolved.

### Why a temporary failure becomes a permanent replay gap

`IngestHandler.onEvent` clears **both complete queues** at the backlog limit,
resets the bloom filter and sets `flushed_cursor = pending_cursor`. Its message
says “dropping oldest events,” but it discards the whole queued backlog.
A later successful POST persists that advanced cursor. The ordinary restart
path therefore skips the discarded interval. The watchdog does not prevent
this: occasional successful flushes reset its consecutive-failure counter,
and queue clearing can happen before its failure threshold.

`handleIngest` commits chunks of 250 SQL statements in separate transactions.
A client timeout or a failure in a later chunk can leave earlier chunks already
committed. Some discarded queue entries may therefore already have reached the
database. Counting them all as missing actors would be incorrect.

### Recovery coverage and limits

The discarded entries can include bare account discovery, profile changes,
identity hints and inactive-account deletes. Later activity may rediscover an
account; identity work also has an independent queue. These paths can repair
some current state, but do not prove full recovery.

The home profile-enrichment flow selects existing rows with
`profile_checked_at = 0 AND handle != ''`. A dropped profile event may never
reset that flag. A missing actor is absent from this selection altogether.
The flow marks not-found profiles checked; it does not reproduce every skipped
inactive-account delete. Building a new search snapshot from Turso also cannot
repair source records that never reached Turso.

We do not have the discarded DID/event lists in these logs, nor evidence
linking each discarded entry to a later successful write. **The number of
accounts still affected is unknown.** Success-log `cursor=` is the current
pending cursor, not an exact identifier for every event in the flushed batch;
it must not be used as an exact recovery boundary.

### Concrete repair direction

1. Preserve replayability when the backlog fills: stop consuming or reconnect
   from a known successfully committed cursor, rather than clear and advance.
   Test failed deletes, partial batch commits, reconnects and bounded memory.
2. Log transport error names and emit queue depth, last successful write age,
   source-event age and discard counters independently of the identity queue.
3. For historical repair, recover an affected-account set from retained relay
   data if available, then reconcile **current** profiles and account status.
   Do not send historical profile values directly through the current UPDATE
   path: it has no event-version condition and can overwrite newer values.
4. Attribute the later write stalls using retained Worker/database telemetry
   if available. Database saturation is proven earlier; extending that finding
   to every later timeout would be an inference.

This follow-up changed documentation only. No production replay, database
mutation, service restart or deployment was performed.

## Mitigation deployed September 5

Active goal: complete mitigation, verify sustained ingestion, and reconcile the
historical interval where current-state evidence supports recovery.

- The 4,000-entry threshold now blocks relay consumption and drains both queues
  with retries, instead of clearing them and advancing the cursor. This also
  permits the existing arena to reset once the backlog drains.
- Batch cursor advancement conservatively retains a relay commit that spans
  multiple batches. Failed deletes continue to hold the cursor back.
- Startup initializes the handler from the fetched persisted cursor. A failed
  or invalid cursor fetch now prevents startup rather than silently starting
  live. A valid cursor must be provisioned explicitly for a brand-new install.
- Retryable batch failures log warnings; transport failures now log their error
  names. Watchdog exhaustion remains an error and restart path.
- The ingester health response exposes main queue depth, delete queue depth,
  backpressure state/count, last successful-flush timestamp and last observed
  relay event timestamp. These are now scraped by the existing health collector.
  Source timestamps can move backwards between relay event types: treat this
  as observed event age, not a proof of complete end-to-end coverage.

Validation: 129 Zig tests passed, including retained backlog after failures,
failed delete cursor safety and a split-commit retry; ReleaseSafe compiled
locally and on Fly. Four health collector tests passed. Deployed only the
Typeahead ingester, latest image
`deployment-01M1SJYTFV149RK3QZBDYZ1DGT`, digest
`sha256:e2b88206559516e33ccc4916912df35d5ba94deb24d6a695034d33feaa1b5c94`.
The first deployment fixed ingestion; the second corrected the health-only
handler actually used by the ingester role. The search app was not deployed.
Prometheus was verified to contain all six new ingestion series. Post-restart
catch-up is still being observed; a healthy HTTP response is not the final gate.

### Historical reconstruction in progress

A read-only subscription at sequence 33310380734 still returns historical data
from September 3 20:16 UTC. The logged September 4 failure times therefore
include replay of older changes, not just changes made during the race.
`scripts/capture-ingest-recovery.py` captures candidate DIDs and reason flags
through sequence 33314565292, a conservative superset of the overflow interval.
It stores no historical profile values and does not write to production.
Local capture: `/tmp/typeahead-recovery.sqlite`, restartable from its checkpoint.

`scripts/audit-ingest-recovery.py` performs paced, indexed DID lookups in Turso,
storing presence and freshness fields in `/tmp/typeahead-recovery-audit.sqlite`.
These are read-only upstream operations. Missing rows can be legitimate deleted
accounts; current account status must be checked before restoration. Neither
presence nor absence proves a particular historical queue entry was lost.
No historical reconciliation writes have been made yet.

### Catch-up follow-up

A third ingester deployment (`deployment-01M1SKFCYY8R1WTA6QT5YKDM5S`,
`sha256:ddb04e582d9ca7db82077db1ce25782e98ac3a788f39f8d18ca31aa7ac097daa`)
changes the five-second partial-batch timer to start at completion of the prior
flush. Previously a slow HTTP write expired the timer while it was running,
causing the next single event to trigger another write. Full batches remain
immediately eligible. A regression test covers both partial-batch collection
and immediate full-batch flushing; 130 Zig tests pass and ReleaseSafe builds.
Observed production logs now show consecutive 400-entry batches rather than
alternating 400/1-entry calls. Catch-up throughput remains under observation.

Current relay status checks are running separately, stored as `repo_status` in
the audit database. Both active missing accounts and legitimately inactive
accounts exist among the candidates. `POST /admin/enrich-profiles` is the
existing canonical current-profile writer; it applies moderation overrides and
blocked-domain policy. Historical values will not be replayed through it.
The database audit was temporarily paused at approximately 20:22 UTC to measure
whether its read traffic contributes to the slow ingest writes; relay capture
and public status checks continue independently.

At 20:22:25 UTC the deployed ingester logged a real `ResponseHeadTimeout`.
At 20:22:29 its 4,000-entry queue triggered backpressure, retaining 4,000 ingest
entries and one delete. Health snapshots subsequently showed 2,000 queued,
then backpressure inactive and the queue below one batch. This is production
confirmation of retained-and-drained backlog under the failure being mitigated.
The audit pause did not produce an immediate catch-up recovery; resumed it with
a two-second delay between 250-DID read batches. Current Fly metrics showed
zero CPU throttling and roughly 1.4% user/system CPU execution, so more CPU is
not justified by this bottleneck. Upstream write latency remains to be localized.

### Shared-writer contention intervention, 20:36 UTC

Worker version `0df1882c-d6c6-4d4e-b8b2-9b818c6319f0` adds database and
cursor timing to `ingest_done` and Server-Timing. All 88 Worker tests and
post-deployment smoke checks passed. Before intervention, 400-event writes
spent 4.2–8.5 seconds in the database and 190–275 ms writing the KV cursor.
The home profile-enrichment flow had started at 20:00:15 UTC, processing
approximately 500 accounts every 30 seconds against the same database writer.

Cancelled run `5f8abbbb-d78a-4511-b978-cf395527b4af`; Prefect confirmed
CANCELLED at 20:36:09 UTC. Remaining rows stay in its self-consuming queue.
After cancellation, a 400-event batch took 436 ms in the database; other
samples were slower, so this is not a sustained latency percentile. Observed
event age fell from 10–12 minutes to 208 seconds by 20:42 UTC. This supports
bulk-write contention as a material contributor; no CPU capacity changed.

A local, not-yet-deployed enrichment guard now checks ingester health before
bulk writes, yielding when event age >=120 s, writes are stale >=60 s, either
queue reaches 400, backpressure is active, or health is unavailable. Waiting
counts against the flow deadline. Fifteen focused tests cover health gating
and existing Turso retries. This lives in my-prefect-server; its publication
requires approval in that repository and has not been performed.

Historical capture completed: 4,184,560 frames, 409,824 candidate DIDs,
ending exactly at sequence 33314565292. Presence audit and current relay
status verification continue. No historical reconciliation writes yet.

### Serving safety gate, 20:44–20:48 UTC

Nate explicitly prioritized continued service during Race to 38. Created a
read-only five-minute health heartbeat. Paused the audit process (PID 35354,
confirmed stopped with SIGSTOP, checkpoint preserved) at approximately 20:44
UTC after checking HTTP P95 against its earlier ~23 ms baseline. No recovery
writes have run. The enrichment guard has 16 passing tests, including deadline
exhaustion without a final flush, and awaits approval to publish in the separate
my-prefect-server repository.

Search spans confirm real slow searches, coincident with overlay apply batches
lasting 3–5.5 seconds. Fly disk I/O wait also spikes during these periods;
CPU steal is small and throttling remains zero. WAL read connections are
separate from the writer; a direct shared-mutex explanation is not established.
One-minute HTTP P95 returned to ~22–25 ms between bursts, with a 779 ms sample
at 20:43. Continue observing a full sync cycle before resuming investigative
load. Ingestion reached observed event age zero at 20:45 UTC.

At the audit pause: 264,250 of 409,824 candidates checked. Current relay status
classifies 241 missing candidates active, 904 missing inactive, and 85 present
accounts inactive (requires moderation-policy review; do not blindly delete).
3,915 present candidates had profile or identity events. These counts are not
loss attribution. `scripts/plan-ingest-recovery.py` reads the local databases
and reports unfinished work without issuing any network calls or writes.

At 20:49 UTC the next sync cycle completed with search ready, overlay lag
60 seconds, ingestion current, and recent one-minute HTTP P95 22–27 ms.
Resumed the read-only audit from its checkpoint at ~20:50 UTC, now with five
seconds between 250-DID queries and a fail-closed public health check every
30 seconds. The guard requires search ready and overlay lag <300 s, ingestion
queues below 400, no backpressure, recent source events (<120 s) and writes
(<60 s). Database authentication is scoped to the Turso POST only. Mock checks
verified healthy, not-ready, stale, and HTTP-failure cases and absence of auth
on public health requests. This guard complements, not replaces, the latency
monitor. Recovery writes remain off.

### Small-batch recovery preparation

Read-only appview checks found current profiles for 244 missing, relay-active
candidates; one active candidate had no profile returned. Stored only presence
and check timestamps in local `profile_available`, not profile values.
`scripts/reconcile-ingest-recovery.py` defaults to a five-account plan. Applying
requires a fresh operator latency/error review plus automatic serving gates;
it rechecks database presence and current relay status, inserts only still-
missing active DIDs through canonical ingest without a cursor, then invokes
canonical current-profile enrichment. It never deletes accounts. The existing
ingest endpoint marks restored actors recently active; this should not be
interpreted as new user activity in recovery reporting.

The local restoration ledger is written before mutation. Tests verify that a
closed health gate issues no mutations, and a lost HTTP response after insert
can resume enrichment without reinserting or sending a relay cursor. Both
passed. Dry-run selected five candidates; --apply has not been run. The broader
audit continues at its reduced pace while publication approval for the Prefect
guard remains pending.

### First recovery canary applied

At ~20:56 UTC, reviewed current serving metrics: five-minute HTTP P95 23.5 ms,
1,093 observed responses all status 200, no CPU throttling, search ready and
ingestion current. Paused audit PID 42345 (confirmed T) to avoid concurrent
investigative database load. Ran the recovery tool with --limit 5 --apply.
All five still-missing active candidates were inserted and enriched, with no
per-DID errors. Immediate health gate passed. An independent indexed Turso
read verified five rows, five nonempty handles, and five recent profile checks;
none was hidden by the canonical moderation policy. Local checkpoint states
are enriched; verification rows are saved at /tmp/typeahead-restored-canary.json.

Do not infer historical loss attribution from this restoration. Next gate:
verify these accounts appear in the serving overlay after the normal sync
and observe latency before expanding recovery. Audit remains paused during
this canary observation.

By 20:58 UTC all five canary accounts were found through direct search queries
for their current handles; round-trip probes were 36–85 ms and Fly one-minute
HTTP P95 was 21.5 ms. Expanded once to 25 selected candidates: one had already
appeared through normal ingestion and was left alone; 24 were restored and
enriched. Health gate passed afterward. A separate indexed query verified all
29 restored accounts have handles and fresh profile checks. None is hidden.
Checkpoint totals: 29 enriched, one already present. The latter is direct
evidence that an earlier absence audit is not a permanent-loss ledger.

At ~21:00 UTC HTTP P95 was 22.6 ms. Resumed the paced, health-gated audit
(PID 42345); no further recovery batches are running. The additional 24 accounts
still need a serving check after the next normal overlay sync. Verification
rows saved at /tmp/typeahead-restored-profiles.json.

### Identity discovery coverage

All 29 restored profiles were found by direct current-handle search after the
normal sync; five-minute HTTP P95 remained ~23.2 ms.

A separate identity POST timed out at 21:00:54 UTC. Code review found that
handle-less identity events only entered the best-effort resolver pool; queue
overflow or a failed resolver POST could therefore prevent even a bare actor
row from being stored. `handleIdentity` now always queues one DID event on the
main cursor-protected retry path, using a valid optional handle hint when
present. The async resolver remains an optimization. This guarantees discovery
has the same retry behavior as the main queue; it does not guarantee immediate
handle/PDS freshness if resolution fails for an already-known account.

Three regression tests cover absent handle with a failed-write retry,
handle.invalid, and exactly one event when a valid hint is present. All 133
Zig tests passed; ReleaseSafe built. Ingester-only deployment started after
pausing the audit again. Previous image for rollback is
`deployment-01M1SKFCYY8R1WTA6QT5YKDM5S`. No search deployment or recovery batch
is concurrent with this rollout.

Identity discovery fix deployed at 21:08 UTC as
`deployment-01M1SP6X02DA2XYR0HE0ZR3YZJ`, digest
`sha256:b2b717af9529e2a3653b3137152aa08620010e756fe9df92acaecfa239743169`.
Startup logs confirm subscribeRepos?cursor=33366401405, followed by successful
400-entry catch-up batches with advancing cursors. By 21:09 UTC source event
age was zero, queue 146, no backpressure, identity queue zero. Search five-minute
HTTP P95 was ~23.2 ms. The recent timed-out identity DID was independently found
in Turso with both handle and PDS: the log alone did not demonstrate a lost
write. Discovery is now retained even when the fast identity writer fails.

### First verified cohort restored

A bounded controller ran batches of at most 25, 30 seconds apart, with fresh
Fly P95 and response-status telemetry before each batch and ingester/search
readiness checks inside the recovery tool. It stops on P95 >=75 ms, any 5xx,
missing/stale telemetry, per-DID errors, or a closed readiness/freshness gate.
The audit remained paused throughout. All batches passed; P95 stayed ~23–24 ms
and observed Fly responses were all successful.

Ledger now records 237 enriched accounts and seven that had already recovered
through live ingestion. A separate Turso point query verifies all 237 rows
have handles. The one candidate without a current appview profile now returns
relay status deleted; it was recorded now_inactive and was not restored.
The relay's end sequence 33314565292 was independently read and maps to
2026-09-03T23:13:38.421Z (start is September 3 20:16 UTC). These historical
changes were being processed during September 4's failure wall-clock window.

At 21:18 UTC: HTTP P95 23.0 ms; 771 observed responses all status 200; both
apps had zero CPU throttling. Memory pressure was ~22.7% search /31.3% ingester;
search disk available ~24.7 GiB. Ingestion was current with queue 155 and no
backpressure. Resumed the audit from its checkpoint. The newest restorations
await their normal five-minute sync before serving verification.

Serving verification for the full 237-account cohort is running sequentially,
one exact-handle query per second, after the normal sync watermark passes the
last restoration. It checkpoints found/not-found locally in search_verification
and stops on failed readiness/freshness checks or HTTP errors. These are direct
Fly probes labelled typeahead-recovery, not measurements of real user traffic.
The read-only presence audit continues separately at its reduced pace.

### Serving verification complete for the first cohort

Direct search verification initially found 236/237. The remaining profile was
returned by the appview as handle.invalid, while current Slingshot resolution
returned a valid handle and PDS and the relay reported active. Refreshed that
identity through canonical /admin/ingest without a cursor, then confirmed it
in search after normal sync. The local verification ledger now records 237/237
found. This does not claim permanent handle freshness: the appview/identity
sources disagree, and canonical profile enrichment can still overwrite a
handle from the appview. Preserve that as a separate follow-up risk rather than
calling handle.invalid a successfully verified handle.

At ~21:25 UTC HTTP P95 was 22.7 ms; all 1,002 observed Fly responses were status
200. Memory pressure ~23.5% search /31.8% ingester, no CPU throttling. Remaining
work includes the unfinished presence audit, recovery of newly verified missing
accounts, and evaluating current fields for historical profile/identity
candidates already present. Presence alone does not prove field reconciliation.
The Prefect guard remains local pending project-specific publication approval.

### Existing-field comparison and interruption checkpoint

Compared the first 100 existing profile/identity candidates with current
appview handle, display name, avatar, description and banner. Initially 63
matched and 37 differed (not a random sample or loss-attribution estimate).
Refreshed those 37 through canonical enrichment after current relay/presence/
source checks, in batches of 25 and 12 while the audit was paused. No errors;
36 now match, bringing comparison totals to 99 matches and one difference.

The remaining difference is an old banner. Current PDS profile record CID
bafyreieqgt3yzekvr4wsgfsxaomvjprqjk5lyxgpb5com3a7lqyfooycbi has no banner.
Canonical enrichment preserves an old banner when the new value is empty.
A proposed single-row compare-and-set clear was tested locally to preserve a
newer concurrent update; production Turso rejected it with code BLOCKED. Do not
claim it was cleared. No access escalation has been performed. Evidence and
prior-row backup are /tmp/typeahead-banner-clear-evidence.json and
/tmp/typeahead-banner-before-clear.json. The reason for BLOCKED needs further
inspection before deciding how to proceed through an authorized write path.

The heartbeat interruption was read-only and found serving stable. At resume,
audit PID 42345 was confirmed paused, with 324,000 candidates audited and 85,824
remaining. Resumed that process (same checkpoint) and started the remaining
read-only profile comparisons with five seconds between 25-DID batches and
serving-health gating. No recovery writes are running concurrently.

### Continued recovery and a separate search-tail finding (22:20 UTC)

The presence audit reached 338,500 of 409,824 DIDs (71,324 pending). It and
current-profile comparison are currently paused via SIGSTOP (Python PIDs
42345 and 56733). No recovery writer remains running. The public relay status
reader may continue; it does not touch production storage.

Another 100 existing profiles were refreshed in four 25-DID batches through
`/admin/enrich-profiles`. Across all existing-profile refreshes, 133 now match
current appview fields and four need further review. The separate 237-account
restoration cohort remains verified in search. 77 additional absent-at-audit,
relay-active DIDs are identified; 75 have current appview profiles and two do
not. None of that new missing cohort has been restored yet.

The failed one-row banner clear used a token whose locally decoded capability
is `a=ro`; no credential value was printed. The canonical enrichment SQL
preserves empty descriptions/banners, so those differences cannot be resolved
by repeating enrichment. No token was minted and no access policy changed.

Stopped the paced profile writer between batches at 22:15 UTC after checking
beyond P95: P99 was about 34 ms but P99.9 about 3.5 seconds. Logfire confirms
2–4 second searches well before this recovery pass (including 19:33, 19:53,
20:05, 20:26 UTC), and not exclusively during overlay synchronization.

One read-only `/debug/search?q=bs&limit=5` request took 4.008 seconds and showed:

- `h:bs`: 300 snapshot candidates, 456 overlay entries
- `e:bs`: zero / zero
- `d:bs`: 300 snapshot candidates, **332,783 overlay entries**
- `n:bs`: 300 snapshot candidates, 377 overlay entries

The search engine materializes the entire domain-prefix overlay and merges
it in memory before capping to 50. This gives a concrete explanation for this
query's tail latency; it does not establish the cause of every slow request.
The search service has NOT been redeployed.

Prepared a local bounded union lookup in `index_search.zig`: fetch the top 50
additions for each non-exact key, plus every overlay row that changes a base
candidate, preserving tombstones and lowered scores. Intersections and exact
handle promotion retain full lookups. No new index or schema mutation. Debug
counts now explicitly mark bounded overlay reads. Existing producer stores
scores as widened f32, matching the merge ordering.

Local in-memory SQLite prototype: 100 randomized multi-key equivalence cases
passed; a 332,783-row fixture returned 350 rows instead of 332,783 with identical
top-50 output (about 21 ms vs 82 ms locally; **not a production benchmark**).
Real SQLite/Zig regression covers deletion, lowered score, and tie ordering;
additional selection regression protects intersections/exact promotion. This
is a local candidate, not a verified production latency mitigation.

Latest production check: P95 23.9 ms, 648 recent HTTP responses all 200, zero
CPU throttling, memory about 31% ingester /25% search, disk about24.7 GiB,
ingester source age zero and no backpressure. Search freshness advances on
its normal five-minute sync cycle; the briefly observed 275-second age is
consistent with that schedule, not evidence of a stuck ingester.

22:22 UTC checkpoint: all 135 Zig tests pass and ReleaseSafe build succeeds;
`git diff --check` is clean. Fresh P95 23.2 ms,573responses all200, ingester
source age0/backpressurefalse,searchsyncage94seconds. Resumed the two
read-only audit/comparison processes from their checkpoints after establishing
that the expensive domain-prefix lookup predates the current writes. Recovery
writes remain stopped. The bounded-search candidate remains local only.

### Recovery and recurrence prevention (22:31 UTC)

Resolved the four empty-field differences (two banners, two descriptions)
using the existing `typeahead-turso-token` Prefect Secret block, without
minting a token or changing permissions. Verified the associated URL is the
same Typeahead database. Each clear required an active relay record, empty
current appview field, empty/absent field in the current PDS profile, primary
profile source, and a SQL compare-and-swap on the old field, updated_at, and
source. All four were re-read and compared successfully. All **137** existing
profiles refreshed so far now match the compared appview fields. These are
field comparisons, not a claim that every counter/PDS/label is reconciled.
Evidence/backups: `/tmp/typeahead-empty-field-correction-evidence.json` and
`/tmp/typeahead-banner-before-clear.json`.

Resumed missing-account recovery at five DIDs per30seconds with fresh P99<100ms
as well as P95<75ms, no5xx, and the existing ingestion/freshness gates.
Four batches restored20accounts. The controller stopped before batch5 when
P99 reached214.5ms (P95~23ms, zeroHTTPerrors); no blind retry. All20have current
valid handles in Turso. Search verification is waiting for normal sync in
session41590, using `/tmp/typeahead-new-restored-profiles.json`. Total restored
is now257, with the older237 already verified searchable.

The daily enrichment schedule was still active and had three future runs.
Paused schedule `c578af3b-ad3c-4be8-821e-46b02eab0b3e` for deployment
`7f336f45-0472-4e92-b856-90a6c6468233` using the authenticated Prefect CLI.
The three pre-created runs for Sep6/7/8 were separately cancelled and verified
CANCELLED: `61d21ca3-96fc-42a9-87fb-3d68db567484`,
`b88b8867-4b5e-46b9-b96e-ecf080cf5325`,
`80d2a96e-6b55-4880-b26d-52cfa27a3d2c`.
Added `active:false` only to this schedule in the local my-prefect-server
prefect.yaml; its other edits are preserved. No repository publication.
Re-enable this schedule after deployment of the tested write-health guard;
the backlog remains in the database while paused.

Inactive-policy review rechecked110historical account-event candidates:
107stillpresent;26deactivated,40deleted,41takendown. All107primary-source,
unhidden,noexplicitoverride. Three no longer present. **No deletions made**;
takedowns remain separate from deletion/deactivation reconciliation.

Latest full health: P95~23.5ms,902responsesall200,zeroCPUthrottle,ingester
sourceage0/backpressurefalse,searchsyncage12sec,memory~31%/25%,disk~24.7GiB.
Readonlyaudit/comparison continue. No recovery writer running; no search deploy.

Search verification session41590 completed: **257/257 restored DIDs found**
via exact-handle searches after normal sync. No pending search-verification
job for this cohort remains.

### Inactive account reconciliation (22:40 UTC)

Added `scripts/reconcile-inactive-recovery.py` (plan by default, at most5DIDs)
and two eligibility regressions. It rechecks relay deletion/deactivation,
primary-source policy, absence of overrides, current row contents, and serving
health; backs up full actor rows locally; uses the canonical `/admin/delete`
endpoint without a cursor; then verifies absence from Turso. Backups remain in
`inactive_recovery.backup` in the audit SQLite file, including across uncertain
retries. Takedowns/reactivations/unknown status/alternate sources/overrides are
excluded. The database endpoint also writes canonical tombstones and records
actual actor-count deltas.

A five-account canary plus five5-account batches removed30confirmed inactive
records. All30were verified absent from Turso with no errors; P99checks before
batches ranged43–80ms, P95~23ms. Search absence verification is live in
session50083 (`/tmp/typeahead-verify-inactive-search.py`), waiting for normal
sync before exact-handle checks. Controller9285 has completed its fifth batch
and may still be in its final30-second delay; do not restart it blindly.

Rechecked two unavailable restoration profiles: `qum3irqlwhzbyzvml5ne7wzg` is
now available and eligible for the next current-state check;
`u4hg3ujuhszoxgetx4vlak5e` is now deactivated and is marked `now_inactive`, not
restored. No source-policy bypass to work around appview unavailability.

Audit at388500/409824; current profile comparisons5500, including2053
remaining differences. These are current reconciliation candidates, not proof
that each difference was caused by a dropped historical event. Read-only
audits continue. Latest production P95~22.8ms,745responsesall200,no throttle,
ingestioncurrent,no backpressure,disk~24.7GiB. Bulk enrichment stays paused;
search optimization remains local and undeployed.

Inactive verification session50083 finished:26/26removedaccounts with usable
handles are absent from exact-handle search after sync. The other4had empty
handles; all4have current tombstones verified in Turso (three also had empty
display names). Saved `/tmp/typeahead-inactive-no-handle-tombstones.json`.
Do not call this30independentsearchqueries: coverage is26queries plus4
verified tombstones/database absence. Controller9285 is terminal/successful.

Fresh inactive-policy review found2reactivations, which are excluded from
removal. Its local rows include already-removed candidates; use the
inactive_recovery join and a current row/status check, not raw policy-table
counts, when determining the next batch. No recovery writer is running at
this checkpoint.

### Full presence audit completed (22:52 UTC)

Capture, presence audit1600, initial status reader12439, and current-profile
comparison91118 are all terminal/successful. Presence checked all409824DIDs;
1774were absent at their individual audit times. This is neither a loss count
nor a current missing count. The two unverified statuses were retried and
resolved to deleted/deactivated; no unresolved relay lookup in that cohort.
Current-profile comparison recorded6075observations:3722matches,
2306differences,23appview-unavailable,19identity-review,5no-longer-present.
The matches include137already-reconciled profiles. Exact historical field
values were not retained, so differences cannot be attributed automatically
to the dropped queues. PDS/counters/labels were not part of this comparison.

Retained consistent SQLite backups and five supporting evidence JSON files
under `.codex/recovery/2026-09-05/` (gitignored, directory0700, data0600).
SQLite `quick_check` passed; manifest contains hashes, sizes, timestamps, and
row counts. The saved audit now includes all409824presence observations.
Refresh the backup again after later recovery writes; live checkpoints remain
in `/tmp/typeahead-recovery-audit.sqlite` for the running tools.

Updated plan summarization to account for completed inactive removals instead
of re-reporting stale initial presence. It also works before the action tables
exist. Seven recovery tests pass with `uv run --with httpx --with pytest python
-m pytest ...`; an initial unittest discovery attempt lacked pytest and was
corrected, not treated as a passing run.

Inactive reconciliation exhausted its current eligible queue:78confirmed
removed/verifiedabsent in Turso. Controllers85524,75771,88516 are terminal.
85524stopped before batch5's write checkpoint; its child stderr had not been
retained, so the reason is unproven. Fresh health checks and unchanged local
checkpoints justified resuming; added final child-error detail to the bounded
controller for subsequent failures. No blind retry after uncertain writes.
Search absence checks now69usablehandles verified; final verifier94249 is
live for the remaining recent removals/handle-less records. Takedowns remain
excluded. Plan reports59remaining historical inactive-policy candidates;
policy review has56takedowns, with absent/current-state exceptions requiring
final reconciliation rather than assuming all59stillpresent.

123active missing-at-audit candidates remain for current-state rechecks.
Started bounded restoration session67425 (five batches of five,30seconds
apart, P95/P99/error/freshness gates). Do not start another recovery writer
while this session is live. No search deployment or repository publication.

### User steering: widen to system performance/resources/cost

Nate asked to move beyond the narrow recovery focus once event loss is in a
good place. The deployed loss mitigation is stable; broad cleanup of all2306
current profile differences is not a prerequisite to that discussion, since
they are not established losses from this incident. Keep those observations
as a separate data-quality backlog rather than expanding this incident into
an open-ended corpus cleanup. Search optimization remains local/unpublished.

Bounded remaining-account restoration session57503 is LIVE, at most20batches
of5DIDs/30seconds, with P95/P99/error/freshness checks. Session67425finished:
23more restored,1already-present,1now-inactive (280restored before57503).
Current first57503batch restored5more. Finish this bounded cohort and verify
its search results. Then focus the incident closeout on verified retry/cursor
mitigation, recovered accounts, explicit uncertainty, and paused enrichment.

All78inactive removals:71handle-based absence checks,7verified tombstones for
accounts without usable handles. No unexplained deletion of takedown records:
`src/cron.ts` explicitly preserves takedowns via Blacksky/PDS fallback when
available. Plan now labels that preserved cohort. Two additional deactivated
historical candidates still need an inexpensive current-presence check:
`did:plc:7oegy6mwk3dgeqddgf7ubyo2`, `did:plc:5ouj6mo4jmgwscg6zuyf4v5b`.

Latest check: P95~23.1ms/P99~38ms,1396recentresponsesall200,noCPUthrottle,
ingestsourceage0/backpressurefalse,searchsyncage39sec. Appropriate to broaden
the discussion now; no need to claim every historical field was reconstructed.

### Bounded recovery stopped safely; broader system review started

The 20-batch controller completed 18 batches, adding 89 restored/enriched
accounts and finding one already present. Total restored/enriched is now 369;
257 were previously verified in search and 112 await the final search check.
All 112 pending records are present in Turso, with one `handle.invalid` needing
separate identity handling. Eight active restoration candidates remain.
P99 rose to 270.5 ms, automatically closing the write gate while P95 stayed
approximately 23 ms and recent responses had no HTTP errors. No writer remains
running. Checkpoints are retained; do not start another writer until the gate
clears. A 14-second incremental search sync coincided with this period, without
establishing causation. Current ingestion and freshness remain healthy.

The two residual inactive-policy cases were checked by indexed DID lookup and
are already absent; their local ledger now records that. The bulk enrichment
schedule remains inactive with zero running/pending/scheduled runs, verified
through Prefect's read API. No Prefect code was published.

See `2026-09-05-system-performance-review.md` for the broader discussion:
verified Fly costs, resource headroom, single-machine availability, expensive
overlay lookups, and per-request database accounting writes. The remaining
2,306 current profile differences are explicitly outside this incident's
bounded cleanup. Turso billing access is expired and current totals are unknown.

### Final verification progress, approximately 23:16 UTC

Authoritative Fly machine listing confirms the expected ingester image/digest
is still running. Logfire returned no ingester warning/error, overflow,
discard, or backpressure matches in the preceding 30 minutes. Source freshness
and queues remain healthy; search P95 approximately 24 ms, no observed 5xx.
Another 25 restored accounts passed exact-handle search, total 282/369.
The resumed read-only verifier waits while P99 is >=100 ms and resumes from
its ledger. No recovery writer is active; the attempted last-eight controller
and identity correction both exited before writing because the gate closed.

The newly identified invalid handle belongs to
`did:plc:tuihodyvf46lpkaxahcry2vy`; Slingshot currently resolves it to
`cookieger.wsocial.eu` on `https://pds.wsocial.network`. The correction is
prepared with live status/identity/health rechecks, but has not run.

At 23:15 the Fly histogram counted 11 of 906 responses above 100 ms. The P99
pause therefore represents a real slow tail, not a unit error. Recent sampled
Logfire spans showed sync apply work, without proving that it explains all
HTTP delays. Keep the gate intact. General search performance remediation is
tracked separately from the deployed event-retention fix.

### Recovery completed, approximately 23:23 UTC

The gate cleared; one identity correction and the final eight restorations
succeeded. Both final restoration batches passed health checks with no errors.
After the normal sync, all 377 restored accounts were found in search. The
verifiers and writer controllers are terminal; no recovery process remains
running. All eligible missing active candidates in the captured set are
resolved. Backups and their manifest were refreshed and integrity-checked.
The final requirement-by-requirement audit and quantified limitations are in
`2026-09-05-ingester-recovery-closeout.md`. Historical losslessness is not
claimed. The paused Prefect schedule remains the operational mitigation until
Nate approves publishing the guarded workflow in that separate repository.

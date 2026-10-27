# Safe performance goal — October 27, 2026

Status: active; native performance changes remain unshipped and transport is under
guarded qualification. Owner: this Typeahead performance goal. Dated state follows.
The starting branch is codex/typeahead-io-audit at 5821bda, with locally tested
write guards and phase instrumentation from faef0c8. Production is a separate
verified release; do not treat the branch or a successful fixture as live.

## Acceptance gates

Keep the two-shared-CPU / 1 GiB serving configuration. Preserve complete actor
responses and ordering, exact promotion, moderation/tombstones, global and PDS
scope semantics, alias equality, freshness and scoped fail-closed behavior.
Compare baseline and candidate against the same frozen snapshot, overlay and
captured updates. Hash those inputs and the case manifest into one input ID.
Keep the append-only canonical ranking cases; add short, Unicode, multi-token,
long, empty-result, unknown-PDS and excluded-PDS cases.

Initial native target: warm mixed-workload p95 <=50 ms and p99 <=100 ms, with
zero timeouts, incorrect bodies or unexpected statuses. Record varied-prefix
first-touch results separately: aim for low-hundreds-of-ms tails and no origin
budget breaches. Failure-inclusive measurements must include queueing under the
offered load and must not omit slow failures, retry them into success, or accept
an incorrect HTTP200 empty body. Intentional error-contract cases remain visible.

The public uncached objective is to substantially improve the recent /stats mean
of 263 ms toward its prior ~169 ms baseline under network-comparable traffic.
Public network latency and native engine latency are separate budgets. The page
is materialized and omits a known failure path, so its mean alone cannot qualify
this goal. Preserve the current cache/freshness policy rather than buying a
latency number with older answers.

Test quiet warm reuse, varied-prefix first touch, actual projection/replay plus
search and health, and bounded connection saturation. Record every rejected run;
no later retry erases an intermittent failure. Structural correctness must pass;
Bluesky parity criteria remain separately reported as currently documented.

## Isolation and resource lifecycle

Production and its rollback Machine, volumes, pointers and images remain
untouched during experiments. No load test, cache eviction, verification scan,
resize, restart, clone or export runs on the serving Machine. No experiment
consumes the rollback backend. Preserve the autonomous snapshot publisher.

The regular cloud refresh a21b0b8e-892b-4fce-b06d-7bee9ebfe784 was still Running
at the initial check. Its ownership takes precedence over managed Fly app
preparation. Do not introduce trial resources into either managed app while that
controller may operate there. Read status directly; do not message the other chat.

Local fixtures: scoped TemporaryDirectory, <=256 MiB of payload, no corpus
copies; stop subprocess groups and remove fixtures on success, failure and
cancellation. The local laptop has only ~33 GiB free, so do not download the
corpus there. Native emulation checks are correctness checks, not Fly capacity
proof. Larger trials need a recorded independent cloud scratch root, disk quota,
resource limits, supervising owner, cleanup path and route/resource guards before
writing data. Keep only small manifests, scripts, hashes, timings and profiles.
Verify reclaimed filesystem space after retiring payloads.

Before final rollout, require the runbook's inactive-slot admission, independent
storage, final-size cached restart, freshness and full semantic probes, followed
by guarded conditional routing and retained rollback. Do not publish a native
image as the scheduled default before it qualifies. A new snapshot appearing
mid-experiment invalidates comparison unless both arms use that same frozen
input; it is not permission to mutate the serving slot.

## Work sequence

1. Build a loopback-only, nonce-identified evaluator and failure-inclusive client.
   Include a frozen-input identity so a target or dataset mix-up stops workload.
2. Validate accounting against real HTTP timeouts, wrong bodies and intentional
   errors, then exercise the actual native evaluator on disposable fixtures.
3. Capture comparable phase and I/O evidence on independent corpus-sized data.
   Separate read-pool wait, prefix lookup, merge, hydration and encoding; separate
   writer lock wait, old-row lookup, scope checks, projection, commit/checkpoint.
4. Optimize the dominant measured costs, starting with exact duplicate writes.
   Retain full row-state and compaction equivalence plus scope/ranking tests.
5. Run the mixed-workload and recovery gates at final resources, compare against
   baseline, then perform the guarded rollout and failure-inclusive observation.

## Harness

Build `cd services && zig build eval-server -Doptimize=ReleaseSafe`. The evaluator
binds loopback, uses a real threaded I/O runtime and requires TYPEAHEAD_EVAL_NONCE.
TYPEAHEAD_EVAL_INPUT_ID identifies the supervisor's frozen-input manifest. Its
/__evaluation endpoint exists only in the evaluation connection entry point;
the production handler does not expose it. Existing evaluation launchers now
provide their own nonce.

`scripts/perf/qualify.py` accepts only an explicit HTTP loopback endpoint or
loopback SSH tunnel, checks nonce/readiness/input ID before workload, and checks
them again afterward. Reference capture requires declared statuses, valid JSON,
optional minimum actor count and an exact first handle. Qualification compares
the complete response bytes with that reference and records every attempt.

Concurrency is capped at eight, rate at ten requests/second, scheduled duration
at five minutes and attempts at 20,000. The supervisor must enforce a process
wall deadline as well. Results include wire time, scheduled-to-completion time
and queue time; p95/p99 gates use the latter so queueing does not disappear.
At least 1,000 search attempts are required for a percentile qualification.
Correctness failures fail the run independently of latency. No automatic retries
or production defaults exist. Run accounting tests with:

`python3 -m unittest discover -s scripts/perf -v`

## Initial verification

Seven tests passed, including the actual native evaluation executable, reference
body equivalence under concurrent HTTP, wrong nonce/input identity rejection,
timeouts retained as failures, incorrect HTTP200 empty bodies, and refusal to
forge readiness on nonempty overlay data. A startup-error test also caught the
process-global I/O runtime being destroyed before Zig could report the error;
the evaluator now keeps that global runtime alive for the process lifetime, as
the production executable does. All test subprocesses and fixture data were removed.

A metadata-only prefix update experiment reduced WAL bytes for timestamp-only
updates from 2.97 MB to 1.90 MB, but doubled mixed replay CPU/wall cost in this
Python/SQLite fixture and worsened score/delete cases. It is not in the native
implementation. metadata-first-benchmark.json retains the rejected/tradeoff
measurement; do not present it as an end-to-end improvement.

## Independent offline corpus profile

Owner: this goal. Host: existing cloud builder root@100.78.52.120; native x86-64.
Read only the already published, immutable archived build
`/var/lib/typeahead-indexer/builds/archive/build-b1792908655-14d5-px05`.
Do not inspect, attach, clone or query the active publisher's SQLite databases.
This offline profile cannot publish an artifact, prepare a production backend,
write a slot pointer or change traffic; production admission remains exclusively
on the inactive managed Fly slot under the rollout controller.

Scratch root: `/var/lib/typeahead-perf/<run-id>/payload`; output receipts only in
`/var/lib/typeahead-perf-evidence/<run-id>`. Payload budget 12 GiB, evidence 16 MiB,
and host filesystem reserve 32 GiB. Copy archived input files to independent
inodes at at most 8 MiB/s; no corpus data traverses the laptop. Check source and
copy digests against the published manifest. Never evict the publisher's file
cache or hard-link mutable trial data into its retained generations.

The supervised transient service runs as typeahead-indexer with a private network
namespace, loopback-only evaluator, read-only system/source trees and write access
only to its own scratch/evidence directories. CPU ceiling two cores, RAM limit
1 GiB, no swap, low CPU/I/O priority, at most 64 tasks and a 90-minute wall limit.
It inherits no Prefect, Turso, R2 or Fly credential environment and does not make
network calls. Reserve/budget breaches stop its process group. Normal/error/signal
cleanup removes payloads after children stop; ExecStopPost repeats cleanup if the
supervisor is killed. Preserve small receipts and verify filesystem reclamation.

This stage compares baseline f67005b with the candidate, using identical neutral
evaluator/threading/identity guards on both. The exact source and binary hashes
must be recorded. The frozen overlay is prefilled through the production projection function with
5,000 captured actors from the immutable legacy twin. Mixed load replays half and
advances source timestamps for half, in 500-actor batches scheduled every five
seconds, while checking full response hashes and PDS acknowledgment. Three
background task slots match production scheduling pressure. A 1,008-case varied
manifest preserves the canonical cases and adds deterministic short prefixes.
Its timings do not qualify the aged production overlay or live Turso sync; storage is cloud SSD rather
than the Fly volume. Mixed-workload and final Fly gates remain outstanding even
if the offline warm numbers pass.

Run 20261027-800a5ec34a01 started at 08:03 UTC; cloud-run.json records its exact
unit, command and uploaded hashes. Its first-touch phase evicts the private
filesystem cache only, without clearing process-local slot caches; baseline
reference capture has also touched those caches. Treat that phase as advisory,
not an equally cold comparison. Fresh-process cold qualification remains open.
Warm and mixed phases follow complete varied-query rounds on both arms.

Production readback at 08:15 UTC: scheduled run
a21b0b8e-892b-4fce-b06d-7bee9ebfe784 completed at 08:03:05 UTC and selected
new build b1793081565-0228-px05 on typeahead-search-refresh / 801539a6551778.
The prior startup slot remains running for rollback. Both retain two shared
CPUs / 1 GiB and native f67005b. Public freshness is healthy with 106 seconds
lag. production-during-trial.json retains route, source, resource and health
readbacks. This goal made no production mutation. The /stats 24h miss mean is
259.20 ms, versus 168.83 ms over 7d; the difference is still unqualified.

## Completed first offline comparison — 08:40 UTC

Run 20261027-800a5ec34a01 completed; the systemd service is inactive and no
experiment children remain. cloud-results/ retains the raw failure-inclusive
reports (gzip, indexed by raw-artifacts.json), process counters and input identity.
All 10,080 measured requests
matched status, complete response hashes and PDS acknowledgments; the ten
intentional excluded-scope responses remain in the accounting. Reference capture
also passed all 1,008 cases.

| Mixed phase | Baseline f67005b | Candidate ffd5d86 |
| --- | ---: | ---: |
| p95 scheduled-to-completion | 13.74 ms | 13.61 ms |
| p99 scheduled-to-completion | 15.83 ms | 15.77 ms |
| Maximum | 83.28 ms | 121.57 ms |
| Completed 500-actor batches | 41 | 41 |
| SQLite changes during measurement | 854,661 | 427,563 |
| Process write_bytes increase | 1,747,161,088 | 1,226,850,304 |
| Process CPU time | 31.41 s | 32.40 s |

The mutation reduction is about 50%, process writes about 30% lower, and CPU
about 3% higher. Both versions already meet the warm/mixed targets on this cloud
SSD with a small overlay; there is no demonstrated latency improvement here.
These single sequential arms do not establish performance on Fly or an aged
1.56 GB overlay. Do not convert reduced process writes into an equal reduction
in volume-billed I/O or public latency. The first-touch phase remains advisory
because its process-local caches were not equally cold.

cleanup.json records 7,576,793,088 bytes of owned payload removed and filesystem
free space increasing from 104,110,546,944 to 111,624,867,840 bytes. Other host
activity can affect that difference. retirement.json confirms no evaluation
children or payload remain, and the uploaded executable copies were removed.
Only small code/manifests and result receipts remain on the host; source archives
and both Fly slots were not modified by this experiment.

## Corrected cold protocol — local tested, corpus run pending

The next harness version restarts the baseline after reference capture and starts
both arms from the same frozen overlay without reprojecting it during startup.
It uses the existing admitted snapshot for those fresh processes. Offline cached
startup requires explicit private-root membership for the snapshot and overlay;
the existing Fly evaluator guard remains. Eight real HTTP/native fixture tests
pass, including cached compact restart, preservation of frozen overlay timestamps
and rejection of a neighboring root prefix.

Before each fresh process starts, the supervisor checkpoints and evicts only its
private database, prefix payload and admission sidecar. Linux mincore records
resident-page counts and rejects a cache reset retaining more than one page
per private file. A real Linux file check passed with zero resident
pages after eviction; residency-test.json retains the result. No global cache
drop or publisher-file eviction is used. The writer lag counter now samples
again after sleeping; the original run's zero lag omitted wakeup lateness and
must not be read as a precise freshness guarantee. These changes were not applied
to the completed corpus run.

## Public routing cost — passive evidence and bounded diagnostics

passive-edge-tail.json retains a 50-second tail: four transport searches took
94–225 ms at one millisecond of Worker CPU. No search requests were generated.
Public invocation wall times include waitUntil work and canceled invocations;
they are not response-latency measurements and cannot be subtracted from native
samples to estimate one request's routing cost.

route-diagnostic.json records four uncached /route reads and paired guard-only
404s on the same HTTPS connection, without touching Fly search. Route reads were
119–374 ms; paired guard responses 62–85 ms. The difference includes scheduling
and network variation, not a directly instrumented R2 span. The source currently
awaits the route read before beginning the origin fetch. This motivates the
separate local transport overlap candidate; it does not prove its live gain.

## Transport overlap candidate — local tested, not deployed

Only GET /search can begin an origin read using a route validated within the
last 60 seconds. It still resolves the route under the unchanged one-second
metadata cache policy before returning any hinted bytes. Both origin and build
identity must match. Otherwise it cancels/discards the hinted response and reads
the selected backend. A failed route read returns SearchRouteUnavailable and
disables further hints until route validation recovers. No stale-response
fallback is introduced. An unchanged route uses one origin request; a changed
route can cause one canceled extra GET. The origin deadline remains three seconds.
Admin writes, health, readiness and diagnostic routing retain their existing paths.

The transport emits Server-Timing for route validation, origin response headers
and selection mode (serial, overlap or reselected). It logs the same phase data
for one in ten calls and every error, with sample_rate, without query strings or
client identifiers. The durations overlap and must not be added together;
total_headers_ms is the elapsed header deadline measurement, not body completion. These fields are on the
transport response; the public Worker currently constructs its own timing header.
The public API bodies, aliases, PDS acknowledgment, cache TTL and failure contract
are unchanged. Public error masking remains open RCA work.

102 Bun tests, TypeScript checking and lint pass. A real HTTP fixture with
120 ms control latency and 160 ms origin latency verifies at least 60 ms saved
by overlap while preserving complete responses. Other tests hold a completed
hinted body until validation, cancel old in-flight requests, discard already
received bodies on origin/build change, preserve scope errors and exercise a
real refused TCP connection. These imposed fixture delays do not predict live gain.

A separate actual workerd runtime test uses local R2 and real loopback HTTP
origins. It checks the unchanged one-second cache window, diagnostic bypass,
route changes, cancellation, malformed routing, suppression of hints after
validation failure, PDS acknowledgment, admin authorization forwarding and routed
health markers. A second workerd target directly fetches a closed loopback port
and returns explicit SearchBackendUnavailable. Its first attempt is retained:
the original fixture gateway converted a refused connection to an HTTP500,
so expecting a network rejection at that boundary was incorrect. The corrected
gateway explicitly returns HTTP502 and verifies HTTP error pass-through; the
separate direct workerd check establishes actual network rejection behavior.
All test servers, runtimes and scratch entry points are removed on exit.

Deployment preparation retains the previous transport version
`e8c21659-0b2c-4cec-beb4-86b4ecf91191` from the latest deployment dated
October 26, 18:41 UTC. Deployment lists are chronological: choose by created_on,
not the first entry. A candidate version upload does not route traffic.
Any eventual gradual rollout must preserve this version, run the smoke gate,
watch actual failure events rather than HTTP200 alone, and stop or roll back on
contract/freshness regressions. Do not claim the public mean target from these
fixtures or the earlier native/cloud measurements.

At 09:34 UTC, source ecd1904 was uploaded as transport version
`4a3b1424-ae73-4e0f-bb58-e838126678f9` at **zero traffic**. Readback confirms
the existing e8c21659 version still serves 100% under the unchanged deployment.
Public freshness is healthy on build b1793081565-0228-px05 with 275 seconds lag.
transport-overlap/staged-version.json records this state. The native write
optimization remains undeployed. Fly cold/mixed qualification and demonstrated
public-latency improvement remain necessary; the goal is active, not achieved.

## Private Fly hardware qualification — preparation

Owner: this performance goal. Create at most one new private Machine in the
inactive managed app typeahead-startup-20261024, at two shared CPUs / 1 GiB,
with its own new 20 GiB volume in ewr. Preserve rollback Machine 080d415b734708,
its volume, configuration and slot pointer; preserve the serving refresh app.
The trial has no public services and cannot receive routed traffic. Refuse
creation if routing or the protected Machine identity changed or another trial
is present. No serving/rollback database is cloned, scanned or exported.

Use the inactive app's existing archived b1792908655-14d5-px05 slot manifest,
without changing it. Installation/admission read R2 directly into the new volume.
This older generation is an offline performance input, never a promotion target.
Capture at most 5,000 actor blobs from actor-namespace page fences in that private
snapshot; this is an explicit biased sample, not a random population estimate.
Both arms consume the same frozen feed/overlay and the same neutral evaluator.
No source/Turso traffic is generated by the measurement process.

Scratch: the new volume's /data/refresh/index/generations/<build-id>, payload
budget 16 GiB with a 2 GiB free-space reserve; evidence /data/evidence, <=16 MiB.
Install plus all phases have a 90-minute Machine lifetime. An independently
supervised cloud manager holds a short-lived app-scoped Fly token, creates only
these new resources, records their exact IDs/owner, and retires them on completion,
failure, cancellation or timeout. It never deletes a pre-existing resource or a
Machine exposing services. Only small scripts/binaries/results cross the laptop;
the corpus stays on cloud storage. Stop child groups before deleting owned
payloads, retain filesystem free-space receipts, then verify Machine and volume
retirement through Fly. Do not declare qualification complete with trial resources
still present. Recheck protected production/rollback identities afterward.

The private image derives from the cached production runtime image and replaces
only its command with a bounded offline evaluator; it is published under the
inactive app registry and is ineligible for the refresh app promotion path.
README reviewed: its performance numbers remain acceptance targets, with corpus
qualification and rollout explicitly pending; no deployed guarantee changed.

The first Fly manager launch, 20261027-7088a95d9d9c, stopped before any
resource creation: the public route rejects Python urllib’s default user agent
with HTTP403 from the cloud host. An explicit typeahead-perf/1.0 agent returns
HTTP200 with the expected route. Its cleanup receipt confirms zero owned
resources. ExecStopPost also exposed a DynamicUser credential read failure;
the retry uses a root supervisor restricted by systemd filesystem/capability
controls, with its short-lived credential retained through cleanup.

At 10:21 UTC, run 20261027-04adaf220602 created private Fly Machine
7841712c141d18 with independent volume vol_4oj6gj0n91km3qxr, then began
R2 installation. Its independently running systemd manager owns retirement
and retains a two-hour app-scoped credential through ExecStopPost. The first
failed launch’s credential was revoked. This active payload is not qualified.

At 10:23 UTC, the previously staged transport version began a bounded 10%
canary, retaining e8c21659 at 90%. The ordinary smoke gate passed; a second
credentialed smoke pass also exercised admin CRUD and Turso selector plans.
The canary watcher automatically restores the previous version on correctness,
runtime or freshness failures, or insufficient overlap samples after ten minutes.
First samples are serial cold starts, so there is no measured live speedup yet.
Only a successful, observed canary may continue; native code remains undeployed.

A 10:30 UTC read of the underlying five-minute metrics (six-hour bounded
indexed query) reports hourly miss means around 199–232 ms; the 09:00 UTC
hour has 509 recorded misses averaging 205.23 ms. This is more recent than
the materialized 24h mean, but still omits masked backend failures and includes
smoke traffic. Do not attribute it to the 10% canary. Recent Logfire native
search sampling (51 spans, 09:30–10:30 UTC) reports mean 10.71 ms and p95
33.04 ms on the serving refresh role. Different cohorts and partial coverage
prevent subtracting these summaries as a precise network-phase measurement.
The explicit transport cold-start samples show routing validation at 58–122 ms
and origin headers at 25–45 ms, identifying measurable routing overhead.

The first live canary exposed an observability limitation: sampling each
isolate’s first and every tenth call preferentially captures cold starts in this
low-volume service. A local follow-up records one small query-free timing event
per transport search (roughly 13,000/day at observed demand). This changes neither
native span sampling nor routing behavior. It is not in the active canary version;
full timing coverage needs its own staged version before another live evaluation.

The first ten-minute canary ended at 10:33 UTC and restored e8c21659 to
100% because overlap samples were insufficient. Eleven timing samples were
serial; this biased sample cannot estimate the overlap fraction. Correctness
smoke passed and freshness remained healthy throughout. Runtime tail outcomes
include client cancellations; they are retained rather than counted as successful
requests. Final deployment readback confirms the candidate receives zero traffic.
The native private Fly trial continues independently.

The complete-timing follow-up passed 102 Bun tests, type checking and targeted
linting, and was uploaded as version 7504eb57-d149-41f3-b1c7-ff0fe91813d1
at zero traffic. It changes timing-event coverage only relative to the first
overlap candidate. A new live evaluation remains pending; e8c21659 remains
the retained serving version.

A second bounded 10% canary uses complete timing events and the same routing
implementation, with the previous version retained at 90%. It requires the
credentialed smoke gate, healthy freshness and no observed backend/runtime
failures, plus at least ten complete timing events and three overlapped calls
before it may remain active. These are mechanism/correctness gates, not a p95
or population-speedup claim; native/public performance goals remain unqualified.
It restores the retained version on failure or insufficient evidence after ten
minutes. Its evidence is in transport-overlap/full-timing-canary/.

Scope correction for the 33.04 ms native p95: deployed f670 starts its search
span after read-pool checkout. It excludes queue wait and cannot establish the
full native request target. The tested local phase instrumentation moves the
span before checkout, but remains undeployed. The private harness measures
complete client attempts and is the relevant admission gate.

The first full Fly run stopped after reference capture: all 1,008 reference
contracts passed, then Python’s mincore helper failed with ENOMEM because its
7.29 GB MAP_PRIVATE mapping requested write access on the 1 GiB VM. No warm,
cold or mixed phase was measured. A read-only libc mmap fixes the harness; a
native Linux test with a 1 GiB data limit reproduces the old failure, succeeds
with the new mapping, observes a written resident page, and confirms its eviction.

The payload reclaimed 7,502,434,304 bytes before teardown. Fly readback confirms
the trial Machine and volume are absent and the retained rollback identity is
unchanged; the run’s token was revoked. The manager’s immediate volume-list
confirmation raced Fly deletion visibility, so it now polls for bounded confirmed
absence. Its cleanup hook also could not read the removed LoadCredential source.
The retry uses a root-only ephemeral RAM-file through both manager phases, then
unlinks it in the final cleanup hook; a real systemd dummy-token test verifies
start, stop-hook access and source removal. No real token is stored in evidence.

The second transport canary also restored e8c21659 to 100%: it observed 18
complete events (16 serial, 2 overlap), with passing credentialed smoke and
healthy freshness. This is insufficient overlap evidence for keeping the candidate
live. It suggests poor per-isolate hint reuse, but does not establish an isolate
lifetime distribution or a population latency gain. Both candidates remain at zero
traffic; the performance goal remains unqualified.

Private retry preparation also removes redundant post-copy MD5 reads from its
rclone wrapper only. Size checks and the unchanged full SHA256 admission remain
mandatory; the production installer’s transfer command is unchanged. The pinned
rclone supports --ignore-checksum, which skips its post-copy checksum pass:
https://rclone.org/flags/ . The retry’s corpus/storage/guest/cleanup bounds remain
the same as the first private Fly run.

At 10:58 UTC, corrected run 20261027-4e76e7937657 started with image
6f7bf0e782ae5bed6215cb1ea96c7884b98df2832f7c5dbde9ce6ef193b65327,
private Machine 85379da4691148 and volume vol_vxmq97z6m565ewj4. Its
independent manager is active and installation is downloading at 10:59 UTC.
The baseline/candidate ELF hashes are unchanged from the first Fly pair; only
the private Python probe/installer wrapper changed. A real offline rclone copy
through that wrapper passed a SHA256 comparison. Production freshness is healthy
on b1793081565-0228-px05 with 141 seconds lag, and final transport readback
confirms e8c21659 still receives 100%. No native optimization is deployed.
README reviewed again: its numbers remain targets and corpus qualification/
rollout remain pending, accurately reflecting this state.

## Shared advisory route hints — local implementation in qualification

The two canaries did not establish enough reuse of an isolate’s 60-second route
hint. The next local candidate stores that same bounded advisory record in a
named Cache API entry so a new isolate can reuse it. It adds no infrastructure
binding or VM. Authoritative route reads, their one-second TTL and origin/build
comparison remain unchanged; cached hints never authorize a response. Metadata
and hint lookup race, so a slow/missing/corrupt hint cannot delay an already
validated route. Records expire within 60 seconds of R2 validation and cached
JSON is bounded to 16 KiB. Validation failure suppresses local hints and schedules
cache eviction until a successful authoritative read. Complete phase events add
hint_source and hint_wait_ms. Admin/health/diagnostic paths remain unchanged.

Affected docs are README’s candidate overview, this plan, the operations status
and the automatic-refresh runbook’s transport mechanism. This implementation is
not deployed; real Cache API persistence/restart and HTTP failure tests, followed
by guarded live qualification, are still required. The isolated Fly trial continues
independently and is not evidence for Worker latency.

The shared-hint implementation passed 104 Bun tests, type checking, targeted
lint and both real-workerd fixture suites. The new suite restarts workerd over
the same persisted Cache API/R2 stores, proves a cold isolate sees the hint,
then changes the authoritative route and rejects the old hinted response. It
checks oversized/malformed/expired/future/unmanaged entries, held HTTP validation,
PDS acknowledgment and R2 failure/recovery. Async-hint tests also prove a slow
hint cannot delay a validated route. No fixture reaches an external origin.

At 11:18 UTC, the Fly baseline cold phase had zero incorrect responses but
p99 1,611 ms, failing the explicit 500 ms cold gate. Its warm and mixed p99
were about 23–24 ms. The candidate warm phase also passed around 24 ms p99;
its cold p99 was 2,705 ms. These are interim results, not full admission.
The private snapshot directory is 31,178,752 bytes and its block-hash sidecar
about 57 MB. Both are lazily mapped. Prefaulting only these metadata mappings
before readiness is the next native hypothesis; it must retain block checks
and leave actor/prefix payload pages cold. No startup-time or memory guarantee
is inferred before a measured isolated trial.

## Completed first measured Fly pair and metadata-prefault retry

Run 20261027-4e76e7937657 completed all 10,080 measurement attempts with
zero incorrect status/body/PDS results. Mixed p95/p99 were 18.08/23.76 ms
for baseline and 19.87/25.66 ms for guarded writes. Baseline ran 40 writer
batches; candidate ran 41. Process writes were 1,916,862,464 versus
1,342,849,024 bytes (30% less total, about 32% less per batch); CPU was
42.78 versus 48.14 seconds (13% more total, with one extra batch). This
shows reduced I/O, not a warm latency win. Both cold phases failed the
unchanged p99 <=500 ms gate: 1,611 and 2,705 ms. Those tails include
offered-load queueing, not only HTTP wire time. Raw attempts and hashes
are retained in the run’s results.tgz and indexed by raw-artifacts.json.

The payload was removed and filesystem free space increased by 7,593,119,744
bytes. The VM is absent; the empty volume is unattached and pending_destroy.
The provider keeps soft-deleted records that flyctl’s default list omits. The
earlier “volume absent” receipt has been corrected to active-inventory absence,
with the actual provider state retained. Pending-destroy volumes do not accrue
charges: https://docs.fly.io/about/billing#volume-billing . The supervisor now
recognizes that inactive state instead of waiting for provider record removal;
real HTTP tests cover soft deletion and idempotent cleanup. Its token was
revoked and its root-only RAM credential source is confirmed absent.

The next native candidate prefaults only the root directory and block-hash
metadata during cached snapshot preparation, each capped at 64 MiB. Larger
metadata retains the existing lazy mapping. On Linux Zig 0.17 implements this
option with MAP_POPULATE; unsupported platforms ignore the advisory option.
Full admission, per-block checksum checks and file identity checks remain intact.
No actor/prefix payload mapping is populated. All 154 native tests passed, with
six platform skips, including corruption/recovery fixtures. This is local evidence,
not a Fly cold-latency claim.

A new private paired run uses the same inactive app, independent 20 GiB volume,
2 shared CPUs/1 GiB/no swap, 16 GiB payload budget, 2 GiB reserve, <=16 MiB
evidence and 90-minute lifetime. Owner: this goal; cleanup: the independent
manager and volume retirement after payload reclamation. The retained rollback
Machine, its volume, R2 slot pointer and serving route are protected as before.
It retains the same input corpus, cases, request rate, percentile thresholds and
reference equality checks. New neutral observer fields report startup time and
post-readiness metadata/payload residency; more than 2 MiB resident payload
before first-touch aborts qualification. Pre-start residency must still be cold.
A native Linux split-residency test confirms the probe distinguishes a resident
metadata page from untouched payload pages without warming them.

The shared-hint version 40a014e4 passed a limited 10% canary with 15 timing
events, seven shared-cache overlaps, median concurrent phase time 39 ms, zero
observed masked backend failures, and passing credentialed smoke/freshness.
Public outcomes include 17 client cancellations, retained as a separate category.
A bounded 50% follow-up is active, retaining e8c21659 for rollback; this still
does not establish the public mean target. Rollback checks deployment ownership
so another writer’s deployment is preserved.

The metadata-prefault trial is enlarged to 200,000 real captured actors,
roughly three days of discovery at the measured arrival rate and larger than
the currently observed 16,657-row production overlay. This is still a biased
fence sample, not a claim that all live delta patterns are covered. An archived
metadata-only binary search found 328,815 actor fence entries, so the requested
count is available without scanning or exporting the serving database. Capture
uses a reusable per-row arena and a <=128 MiB serialized feed; default evaluation
feeds retain their existing 5,000-actor budget unless explicitly raised. Feed
order is deterministically shuffled to spread mixed writes through the overlay.
A real 5,001-row fixture rejects an implicit budget increase, accepts an explicit
one and verifies fields remain valid through native projection and HTTP reads.

Scratch remains the new private 20 GiB volume, payload <=16 GiB, free reserve
>=2 GiB, evidence <=16 MiB and lifetime <=90 minutes. Bootstrap/finished arm
overlays are retired after copying/hash/measurement so only the frozen overlay
and current arm need coexist with the snapshot. Reclamation receipts are retained.
Capture has a ten-minute bound; all measurement rates, latency thresholds,
2.5-second attempt timeout and failure-inclusive accounting stay unchanged.
Only unmeasured reference extraction may wait up to ten seconds per response
to establish the correctness oracle; it remains bounded by its existing total
command deadline. Startup time includes evaluation-feed loading and is not a
production restart estimate. Post-ready residency is saved before its cold-data
gate so a failure retains diagnostic evidence. No source/Turso requests are made.

The shared-hint 50% observation passed with 92 timing events, 57 shared-cache
overlaps, median concurrent phase time 36 ms and zero observed masked failures.
A guarded 100% observation restored e8c21659 when the public passive tail closed.
A retry also rolled back when that observer exited successfully without an error
diagnostic. Neither is a service-failure finding or a completed observation.
Both streams passed a six-minute deployment-free check with stdin held open;
the [receipt](transport-overlap/shared-hint/observer-preflight.json) records
their live handles before teardown. A new 30-minute guarded observation started
at 13:00 UTC with the same held-open stdin, unchanged service gates and public
counter snapshots before/after the observation and after smoke completes.
Holding stdin open is a tested observer mitigation, not proof of the earlier
disconnects' cause. This attempt rolled back at 13:01 UTC after unchanged smoke
failed its second `/stats` fetch with a read timeout. Search checks passed and
no masked failures were observed; neither fact excuses the failed gate. A later
compressed `/stats` read on retained e8c21659 returned 200 in 330 ms (132,364 wire
bytes, 578,803 decoded bytes). The [readback](transport-overlap/shared-hint/stats-readback.json)
does not establish the timeout's cause. The retained Worker remains at 100%
while unchanged smoke was checked there. That smoke passed without changing
timeouts or correctness checks. Another guarded 30-minute observation started
at 13:04 UTC, with the same error, freshness, smoke and observer gates and
deployment-ownership rollback. No latency improvement is claimed.
Public mean improvement remains unproven. At 12:54 UTC, hourly uncached means
were 219, 226 and 242 ms for 10:00, 11:00 and the partial 12:00 hour; these mixed
version/smoke cohorts cannot attribute a gain to the candidate. See
[the bounded counter read](recent-stats-1300.json).

Private run 20261027-a944d3237145 is active on Machine d893105c150108 and
volume vol_v8eyqz0kkkw8mpkv. Its immutable image and independent supervisor are
recorded in [the launch receipt](fly-trial/launch-20261027-a944d3237145.json).
It has no public services and cannot modify the serving or retained rollback
Machine. Qualification and retirement remain pending.


### Observer failure and independent analytics — 13:12 UTC

The post-baseline full-traffic attempt passed unchanged smoke, then restored
retained e8c21659 at 13:08 UTC when the transport tail exited with code zero.
Its 53 timing samples and 32 post-smoke cache misses are insufficient for the
full observation gate. Those misses averaged 186.66 ms; all 32 correspond to
32 additional HTTP 200 counters, with no other status increments. This short,
failed window includes final counter readback after rollback and does not
qualify the public target. Holding stdin open did not prevent the disconnect.
No further rollout is running.

Bundled Wrangler 4.74.0 lacks the reconnect lifecycle present in Cloudflare's
[upstream tail fix](https://github.com/cloudflare/workers-sdk/pull/14415).
Temporary Wrangler 4.153.0 is installed only under
`/tmp/typeahead-observer-wrangler-4.153.0`, without changing project dependencies.
A bounded twelve-minute read-only liveness check is running; CLI liveness is
not proof of continuous log delivery. This tooling hypothesis remains unproven.

The existing OAuth connection can read provider-side GraphQL invocation
analytics: [a bounded query](transport-overlap/shared-hint/analytics-read-probe.json)
reported 1,059 public Worker invocations and zero runtime errors for
12:55–13:10 UTC. This covers every endpoint and cannot identify HTTP-200 masked
search failures. Counter reconciliation and search-specific outcomes remain
necessary. A schema introspection request depleted the analytics query budget;
no further analytics queries will run before the stated five-minute cooldown.
No additional permissions were requested.

Private run a944d3237145 completed its exact 200,000-actor capture and entered
bootstrap admission/prefill. Its measurement and cleanup gates remain pending.


### Large-overlay preparation failure and bounded retry — 13:30 UTC

Run a944d3237145 captured exactly 200,000 actors, then timed out during the
unmeasured bootstrap prefill at its 900-second preparation limit. It never ran
a latency phase. The [failure and retirement evidence](fly-trial/run-20261027-a944d3237145/failure.json)
retains that rejected attempt. At a checkpoint, the baseline process had written
13.47 GB for a 225 MB overlay plus 123 MB WAL and was waiting in rq_qos_wait.
This is bulk fixture construction, not a measured live arrival rate or a
candidate regression. RSS was about 550 MiB; no memory failure was reported.

Payload removal reclaimed 7,817,932,800 bytes by filesystem free-space difference.
The VM is absent; its volume is unattached and pending_destroy. The protected
rollback Machine identity is unchanged. The run token was revoked, its RAM
source is absent, and its empty credential directory was removed.

The retry retains all 200,000 actors and the same native binary pair, workload,
case manifest, response comparisons, mixed writer requirement, 2.5-second wire
budget and latency thresholds. Only unmeasured bootstrap preparation gets
1,800 rather than 900 seconds. Other server startup limits remain 900 seconds;
measured startup and post-ready payload residency remain reported. The inner
trial wall bound grows from 50 to 65 minutes to accommodate that preparation;
the independent manager still stops after 85 minutes and its systemd unit
retains the 90-minute cap. Storage and resource limits are unchanged. This
changes preparation allowance, not a failed latency result into a pass.

Temporary Wrangler 4.153.0 completed its twelve-minute deployment-free liveness
check with both child processes live at teardown. The
[receipt](transport-overlap/shared-hint/observer-new-cli-preflight.json) explicitly
does not prove gap-free delivery. A new guarded full-traffic observation uses
that CLI, rejects announced reconnect gaps, retains all prior service gates and
requires at least 100 post-smoke cache misses reconciled against search HTTP
answer counts. Unknown or unmeasured answers reject continued rollout. Public
latency improvement remains unqualified until that window completes.


### Overlapping observer qualification and native retry — 13:51 UTC

The single-stream 4.153.0 canary passed smoke but restored e8c21659 when the
CLI announced a reconnect. The upgrade provides recovery, not continuous
coverage from one stream. That rejected attempt remains recorded under
transport-overlap/shared-hint/canary100-new-cli/.

The bounded [real-service overlap test](transport-overlap/shared-hint/dual-observer-preflight.json)
passed for ten minutes with two tails per Worker. It deliberately replaced one
client for each Worker, survived six real reconnect episodes while a peer stayed
connected, then proved total observer loss is rejected. It retained 150 unique
public and 196 unique transport invocations, and retired all twelve created tail
sessions. Only lifecycle metadata and counters are persisted; full traces are
hashed for bounded in-memory deduplication and discarded.

The observer uses a SHA-pinned copy of temporary Wrangler 4.153.0 with lifecycle
messages added to stderr. The original package and project dependencies are
unchanged. The guarded rollout starts an observer before deployment, adds its
peer afterward, and rejects total coverage loss. A provider-side runtime-error
query and search-counter reconciliation complement these real-time streams.
Latency reconciliation uses complete five-minute buckets beginning after smoke
and ending at least three minutes before readback; this avoids unsettled
background writes. The whole thirty-minute observation still checks live search
failures, freshness and deployment ownership. At least 100 measured cache misses
and 100 complete transport timing events remain required.

Native retry 20261027-963b8d20811d is supervised by the independent systemd unit
of that name with the typeahead-perf-fly- prefix. It owns Machine 824021f7446768
and volume vol_re1qj7o28ge0xod4. The [launch receipt](fly-trial/launch-20261027-963b8d20811d.json)
pins image 9e92c2ee; its baseline/candidate ELF hashes match the prior pair
exactly. Only the two Python preparation-budget files changed. The rollback
Machine remains protected. Full admission finished and input capture is active;
measurement and retirement remain pending. No corpus data was sent home.


### Native saturation correctness and active observation — 14:00 UTC

The native fixture suite now includes 160 real HTTP attempts in eight-client
simultaneous bursts with seven background task slots occupied, including a live
two-actor projection loop. Every actor and empty response must match complete
reference bytes, and writer batches must advance without failures. All seven
fixture tests passed on macOS and on native Linux x86-64. The Linux unit had two
CPU cores of quota, 1 GiB RAM, no swap, a private loopback network, a 256 MiB
scratch cap and a three-minute outer limit. Its peak fixture payload was
29,082,032 bytes; cleanup removed all payloads and retired temporary binaries.
[The manifest and logs](linux-fixtures-20261027-458502984299/manifest.json) identify
the exact qualified candidate ELF and test sources. An initial local invocation
pointed to the Linux ELF and failed before starting any server; that invocation
is retained separately, and the macOS executable was rebuilt before rerunning.
These are small-fixture correctness/starvation checks, not corpus capacity or
Fly latency evidence. No production behavior changed, so the README overview
requires no additional change for this test.

The overlapping-observer Worker observation began at 13:51 UTC and passed
unchanged credentialed smoke. It remains active with deployment-ownership,
freshness, HTTP/runtime/masked-failure and full observer-coverage gates. The
qualified latency counter window will use complete post-smoke buckets starting
at 13:55 UTC. Native retry 963b8d20811d has reached bootstrap admission/prefill;
its measurement and retirement remain pending.


## Reset goal and current-capacity qualification — 14:40 UTC

Nate stopped the earlier goal and explicitly reset the original performance
objective. The active objective retains the same native/public targets, full
search semantics, current 2 shared CPUs/1 GiB and production/rollback protections.
Observer development is parked in a named Git stash. No more observer features
are a prerequisite to the application work; authoritative provider metrics,
application counters, correctness probes and smoke remain available.

The overlapping-tail rollout restored e8c21659 when coverage was lost. Its
lifecycle evidence later showed identical tail-session IDs issued to separate
clients, so the earlier overlap test did not prove independent backing leases.
No production performance improvement is claimed from those rejected windows.

A one-line writer-cache experiment (20 MiB to 128 MiB) passed native tests and
reduced CPU time in a bounded SQL mechanism model, but left WAL writes effectively
unchanged. Because current Fly CPU use is low and I/O is the measured cost, the
configuration change was reverted. [Model source/results](writer-cache-model.json)
retain exact logical-state and compaction equality; these are not Fly latency
or capacity measurements. The serving writer cache remains 20 MiB.

The production diagnostic at 14:34 UTC reports 12,809,429 snapshot actors and
30,606 overlay actors. The next paired qualification uses the current published
build b1793081565-0228-px05 and 50,000 captured overlay actors, rather than blocking
current-capacity measurements behind a 200,000-actor fixture. It retains the full
published snapshot, canonical cases, exact body/status/PDS comparisons, mixed
projection workload, cold-data checks and original percentile gates. This tests
current capacity with overlay headroom; it makes no 200,000-actor or future-cycle
capacity claim. Broader capacity remains unproven.

Private installation reads the existing serving-slot publication pointer without
modifying it, on a new independently owned volume and private Machine. The serving
and retained rollback Machines and their disks stay untouched. Native binary
hashes remain the 9b65d77c pair: no cache-tuning experiment is included. The prior
200,000-actor retry also stopped during unmeasured preparation; its failure,
cleanup and independent retirement receipts are retained. No latency failure was
converted into a pass. The README targets remain accurate and unshipped.


### Focused qualification in progress — 15:00 UTC

Current-capacity run 20261027-2caf472a9943 is live under its independent manager
(MainPID 2454248 at the check), on private Machine 802e5da6391d78 and volume
vol_vz80lxy65o2p0wjv. The immutable image ce00b57d retains the original native
binary pair and reads the current publication. It is admitting the full snapshot;
no native latency phase has run yet. Its launch receipt and image manifest are
under fly-current-capacity/ and fly-trial/.

The existing transport version 40a014e4 is in a bounded qualification using
provider-side invocation/error analytics, public freshness, exact-handle probes,
credentialed smoke, deployment ownership and settled application counters.
It does not depend on continuous live-log delivery. All gates restore e8c21659
on rejection. The predetermined efficacy gate is mean <=185 ms with >=100
natural uncached searches and HTTP answer counts fully reconciled against
latency/cache counters. This is a milestone toward the original ~169 ms target,
not a change to the goal's native/cold/correctness requirements. The observation
runs thirty minutes plus three minutes for provider aggregation; there is no
production benchmark load.

The immediately preceding retained-version window (14:30–14:45 UTC) had 96
uncached searches averaging 227.01 ms, plus one cache hit. All 97 HTTP-200
answers reconcile. The candidate's first completed bucket (14:50–14:55 UTC)
had 27 uncached searches averaging 169.96 ms and 27 HTTP-200 answers, also fully
reconciled. These cohorts are temporal comparisons on the same deployment
configuration, not a randomized causal estimate. The candidate sample is too
small to qualify. Runtime errors remain zero in the provider readbacks so far;
client disconnects remain explicitly recorded separately. Full-window and
native qualification remain pending.


### Publisher ownership change — 15:18 UTC

The independent guard stopped run 2caf472a9943 when the protected rollback
Machine was explicitly stopped and destroyed via the Fly API at 15:09 UTC.
The private trial retired its own Machine and volume; no native latency phase
completed. The serving Machine 801539a6551778 is unchanged and healthy.

Read-only Prefect inspection identifies running publisher flow
9770e846-b789-4d8c-9cd2-53069daa84d2 (started 13:50 UTC). It is preparing new build
b1793109259-2b36-px05 on managed Machine 8e3d79b70dd748, using the existing b0492f07
production image. Its preparation guest is four CPUs/1 GiB, with the controller's
usual final-size qualification still pending. The performance task will not
mutate the publisher's candidate, route, clock or controller. Native qualification
must resume after this publisher is terminal and the new serving/rollback
inventory has been verified. This is an ownership/state change, not evidence
against the native optimization.

A production-role image for the existing candidate is built but not deployed:
[release manifest](release/native-image.json). Native source is identical to
9b65d77c; no rejected cache experiment is included. The public qualification is
independent of the aborted native trial and remains healthy on the unchanged
serving build at the latest check.

### Qualification using the publisher's admitted inactive snapshot

At 15:50 UTC the publisher is terminal (failed its candidate latency budget),
A is started at 2 shared CPUs / 1 GiB on 8e3d79b70dd748 with independent volume
vol_v8eyqk61q9dy9gkv, and B remains the healthy serving route. The next bounded
trial will fork only A's volume, never B's. Owner: this performance goal;
scratch: /data/refresh on the new private volume; disk budget: 20 GiB with
2 GiB reserve; deadline: 90 minutes; teardown: supervisor deletes only its
owned private Machine and volume. Evidence is retained, corpus payload retired.

The frozen input will use the actual recovered SQLite overlay, including
moderation and tombstones. This avoids synthetic prefill and is evidence for
current actual state, not a 50k/200k aged overlay claim. Neutral evaluators can
now preserve feed row timestamps. The production executable is unaffected by
this fixture support. README's unshipped status remains correct.

The completed transport window 14:50–15:15 UTC contains 205 uncached searches
at 190.95 ms mean and five cache hits. All 210 HTTP-200 answers reconcile.
That is 15.9% lower than the preceding 227.01 ms cohort, but misses the
predetermined 185 ms milestone. The guard restored e8c21659 after an analytics
read timeout; neither this window nor the overall performance goal is qualified.

The frozen-overlay native pair builds successfully for Linux. Seven real native
fixture tests pass on macOS (4.24 seconds), including preservation of actual
feed timestamps during projection; ten HTTP/harness guard tests also pass.
The new private fork is vol_v3gxkkq76w60eem4 on private Machine 894505b6d54718;
it reached reference capture at 15:55 UTC. Source A and serving B are unchanged.
The former cloud supervisor host is offline, so this bounded run is supervised
locally with the same identity/route/ownership checks and mandatory teardown.

Retrospective durable provider analytics cover 14:49:37–15:19:37 UTC: public
2,072 successful invocations and 76 client disconnects, transport 338 successful
invocations, zero reported runtime errors in either service. Disconnects remain
explicit and are not folded into successful HTTP outcomes. This resolves the
analytics-read failure's coverage gap; the public mean still misses 185 ms.

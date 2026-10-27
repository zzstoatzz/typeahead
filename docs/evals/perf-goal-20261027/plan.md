# Safe performance goal — October 27, 2026

Status: active; no production change. Owner: this Typeahead performance goal.
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
100% because overlap samples were insufficient. Eight timing samples were
serial; this biased sample cannot estimate the overlap fraction. Correctness
smoke passed and freshness remained healthy throughout. Runtime tail outcomes
include client cancellations; they are retained rather than counted as successful
requests. Final deployment readback confirms the candidate receives zero traffic.
The native private Fly trial continues independently.

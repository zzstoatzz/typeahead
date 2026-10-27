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

Six tests passed, including the actual native evaluation executable, reference
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
must be recorded. The initial frozen overlay is empty/base-only. Its timings do
not qualify real overlay growth or sync interference; storage is cloud SSD rather
than the Fly volume. Mixed-workload and final Fly gates remain outstanding even
if the offline warm numbers pass.

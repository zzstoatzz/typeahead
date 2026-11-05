# Lifecycle qualification

Status: fixtures passed and the Prefect API retry fix is live, November 5, 2026.
Inactive B completed preparation at 10:16 UTC, and matched full-data comparison
and publisher-owner fixtures passed. The new runtime is serving production on B after guarded promotion at
10:52 UTC. Compatible publisher registration was verified at 11:10 UTC. The
next normal full refresh and obsolete-object cleanup remain pending. The [cleanup plan](../../plans/lifecycle-cleanup.md)
defines completion.

## Verified so far

The [cloud result](cloud-result.json) records 36 passing tests, without skips,
in 40.8 seconds on the [observed guest](cloud-machine.json). The
[log](cloud-fixtures.log) covers real checkpoint/WAL export, import and source
handoff, immutable publication to a local fixture store, corruption rejection,
continuous polling, cached restart and stale-coverage serving with admission
rejection. All source HTTP requests in this run target synthetic loopback
fixtures; the guest has no production credentials or public services.

The exact [source package](package.json) contains 512,596 bytes. The
[preparation script](prepare-image.py) extracts committed source and hashes each
file; the [runner](run-fixtures.py) bounds the child process groups. Build with
`docker build --provenance=false --platform linux/amd64 --network none`, choosing
`--target runtime` or `--target fixtures`. The runtime layers only the slot script
and installer over the qualified continuous-poller image. Its unchanged reader
is `ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d`.
The inherited image revision label describes the base; use the package's file
hashes and source revisions for the overlaid scripts.

The test Machine `85e356c4360178` removed itself after exit 0. Independent lists
confirmed no Machine or volume remained, then the disposable app was destroyed
and confirmed absent. The same packaged tests also passed locally with network
disabled. Local scratch totaled 1.1 MiB; neither the laptop nor HeavyPad fetched
corpus data for this work.

These are fixture results. The later full-data and managed-admission evidence
appears below, followed by verified public cutover and publisher registration.
A normal production refresh remains pending.

The [source verification](source-verification.json) records commit-by-commit
checks for both repositories, plus 93 actual-reader publisher/controller checks
without skips. All three Typeahead CI workflows passed for `6ab4a59`. Publisher
cleanup was first published on both mains at `a4ee1ef`; the subsequent qualified
`0a36b8b` publisher and compatible installer were registered at 11:10 UTC. The [installed image hashes](image-files.json) identify
the exact candidate payload; the [bisect log](typeahead-bisect.log) records all
12 slot tests passing at each Typeahead change.

## Resource boundaries

Owner: this Typeahead lifecycle cleanup. Local scratch:
`/tmp/typeahead-lifecycle-20261105`, at most 5 MiB of source and metadata. Docker
uses the already cached production base; only changed scripts and synthetic test
helpers are uploaded. No corpus or hosting mapping is downloaded locally.

Cloud fixture qualification uses a disposable Fly app
`typeahead-lifecycle-20261105`, two shared CPUs and 1 GiB RAM, with no public
services, production credentials or persistent volumes. `/tmp` holds synthetic
fixtures only; budget 512 MiB. The test supervisor bounds execution to five
minutes and kills its child process group on completion or failure. The Machine
must use restart policy `no` and automatic removal. Fetch its bounded logs and
verify Machine removal, then destroy the temporary app. No recurring observer or
home workload is introduced.

This fixture run does not reserve either managed slot or authorize promotion.
Full-data candidate qualification follows the [preflight](full-data-preflight.md)
after the scheduled refresh, with independent storage and the existing release gates. Production remains
on the qualified continuous-poller image until then.

The root README was reviewed: these qualification steps change neither public
usage nor deployed architecture, so it needs no additional detail.

### Retry ownership and candidate submission

The [Prefect rollout audit](https://tangled.org/zzstoatzz.io/my-prefect-server/tree/main/docs/audits/2026-11-05-in-process-retry-rollout.md)
records the live server fix, uninterrupted API health sampling and an actual
90-second cloud flow retry using one VM and PID. Both worker polling query paths
excluded the owned retry. The temporary VM, IP, SSH key, deployment and concurrency
limit were removed. Concurrency settings, production timeouts and worker source
were unchanged.

The [candidate submission](candidate-preparation.json) is run
`67fc6ac8-b546-4113-bc30-1db8121b273f`, request
`92d56fc206df4304a27c02c987ca2d7c`. The controller allocated inactive B machine
`7841e35f99d338` on independent volume `vol_4ql13kg2ky19w66r` and started it.
It reuses the full refresh's published bundle; no export or import is requested.
Preparation and matched performance passed, followed by release-proof
publication and guarded promotion. A remains retained as rollback.

### Controller recovery fixtures

The [controller result](controller-result.json) records 82 passing tests without
skips, using the actual reader and installer on the observed
[cloud guest](controller-machine.json). Tests ran in 78 seconds; bootstrap plus
execution took 97.7 seconds. The [log](controller-fixtures.log) covers dead-slot
recovery, interrupted provider startup, durable resume, changed ownership and
routing, coverage expiry and promotion verification. These provider failures are
HTTP/filesystem fixtures; no real production provider mutation was attempted.

The separate disposable app, `typeahead-controller-20261105`, ran the committed
controller's tests while the real refresh built. It
has two shared CPUs, 1 GiB RAM, no public service, persistent volume or production
credentials. Its scratch is `/tmp` on the ephemeral root disk: allow 1 GiB for
public Python dependencies and small synthetic fixtures. No corpus is fetched.
The checksum-pinned uv bootstrap and dependencies download directly into that
cloud guest; the laptop uploads only the small test source layer.

The [runner](run-controller-fixtures.py) limits bootstrap and execution to ten
minutes, disables Prefect's ambient API/ephemeral server, and terminates its child
process group. Use restart policy `no` and automatic Machine removal. Preserve
bounded logs and source hashes, verify Machine/volume removal, then destroy the
temporary app. Provider failures use real HTTP fixture servers and owned local
objects; they do not exercise real provider mutations or authorize release.

The [source package](controller-package.json) was 730,164 bytes. During execution,
the cloud uv cache held 471,194,715 logical bytes and the ephemeral root retained
7,513,108,480 free bytes. Dependencies downloaded in the cloud, not through the
laptop or HeavyPad. The Machine exited 0 and removed itself; Machine and volume
lists were empty before app deletion, then the app was confirmed absent. Local
scratch, including both fixture packages and metadata, remained about 2 MiB.

### Publication ownership qualification

The registered publisher fix at
`0a36b8bfe2299a29e87f54c51ed420d341f7a007` preserves the generation's original
`publication_execution_id` when another execution takes over its journal. A
same-run full-refresh retry must still emit ordinary `Completed`; a separate
recovery must not reset the missed-refresh clock. Manual preparation and
promotion used their original `2daeb97` pin; the scheduled publisher now uses
the qualified owner fix.

Before activation, the [owner fixture runner](run-owner-fixtures.py) ran in
temporary app `typeahead-controller-owner-20261105`, owned by this cleanup. It
uses the existing runtime image and small committed test sources, two shared
CPUs, 1 GiB RAM, no public service, credentials or volume, and `/tmp` scratch
bounded to 1 GiB for cloud-downloaded dependencies and synthetic data. Bootstrap
and tests have a 600-second bound; the runner requires zero failures and skips.
It emits bounded, structured JUnit failure summaries so cloud log truncation
cannot hide the failing test names or final errors. FastAPI and Starlette use
the tested operations-repository lock versions (0.141.1 and 1.2.1); newer
unconstrained FastAPI resolution rejected Prefect 3.8.2’s test-server import
before the two actual-engine tests could run.
Use restart policy `no` and automatic Machine removal. Preserve source hashes
and bounded logs, verify empty Machine/volume lists, then destroy the app on
success or failure. The local source/metadata budget remains 5 MiB; no corpus
data is fetched locally or on HeavyPad.

## Matched comparison

The [comparison tool](compare.py) implements the [preflight](full-data-preflight.md)
cohort and bounds. Run it in cloud infrastructure with the actual machines and
their shared build after managed qualification, using a new output path for each
attempt:

```sh
python3 docs/ops/lifecycle-cleanup-20261105/compare.py \
  --baseline https://typeahead-tantivy-a.fly.dev \
  --candidate https://typeahead-tantivy-b.fly.dev \
  --baseline-machine BASELINE_ID --candidate-machine CANDIDATE_ID \
  --build-id SHARED_BUILD --output NEW_RESULT.json
```

It requires explicit managed origins, permits no redirects, and issues at most
39 requests per target at one request per second per target, with a five-minute
execution bound. Every response must acknowledge the expected engine, format,
binary, build, retrieval, machine and scope. Global requests cannot acknowledge
a PDS scope. Changed instances, errors, mismatched bodies and incomplete cohorts
fail qualification. Results contain hashes and timings, not full profile bodies;
existing output files cannot be overwritten. Sampled p95 uses nearest rank.

The [cloud wrapper](cloud-compare.py) supervises the comparison process group for
315 seconds, removes its temporary result directory, and emits compressed,
numbered evidence chunks with a SHA256. Reassemble and verify those chunks before
removing the temporary app. The [real HTTP wrapper test](test_cloud_compare.py)
passed both a complete paced cohort and rejected identity in 40.2 seconds,
including result framing and scratch cleanup. These are fixture results.
The [preflight](full-data-preflight.md) declares the actual cloud client's owner,
image, resource limits and cleanup before it is allocated.

The [real HTTP tests](test_compare.py) passed all four cases in 81.6 seconds;
see [the test log](comparison-tests.log). They exercise the full paced cohort,
reject a fast network response that reports a slow engine, reject unexpected
scope and identity, preserve failed output, and stop redirects after one request.
Loopback execution requires `--test-loopback` and always records `test_only: true`;
it cannot substitute for the real-backend result recorded below.

## Candidate submission

From the operations repository, use [the preparation helper](prepare-candidate.py)
with the completed full refresh's actual run ID:

```sh
just prefect --script ../typeahead/docs/ops/lifecycle-cleanup-20261105/prepare-candidate.py \
  --refresh-run REFRESH_RUN_UUID \
  --output /tmp/typeahead-lifecycle-20261105/candidate-request.json
```

First run without `--submit` to record the proposal, then rerun with `--submit`
to launch the cloud preparation. It requires the exact refresh to be completed,
its build to be serving from A with at least 18 hours of coverage, no routing
mutation or pending slot operation, an in-budget immutable bundle, and green
main CI for the pinned controller. It reuses that artifact on inactive B, through
the existing controller and previous-slot retirement gates. It never promotes.

Only this run's publisher/source/installer overrides change; the scheduled
deployment remains pinned to its existing release. Credentials and complete job
variables stay out of the local proposal. A persisted submission intent, stable
request tag and API idempotency key reconcile lost acknowledgments. A missing
execution or unknown outcome refuses another submission; inspect that request
instead of allocating again. An existing failed run requires explicit controller
recovery, not edits to the local record to force another allocation.

The [guard checks](preparation-guard-tests.json) exercised real read-only API
responses: the active full refresh was refused, and unknown/missing execution
records required reconciliation. No candidate run or production mutation was
created by those checks. The later positive preparation is recorded below.

## Offline import diagnostic

The [bounded sample comparison](import-tuning.md) reused 100,000 actors already
on the disposable cloud builder. Its [receipt](import-pilot-results.json) records
matching results, timings, resource bounds and scratch cleanup. Larger batches
were promising; production settings remain unchanged. The [pilot script](pilot-import.py)
is tied to that run's frozen sample and must not be used as a full-corpus gate.

## Prefect cache readback

At 07:36 UTC November 5, the [read-only cache verification](cache-verification.json)
loaded and deserialized all four completed stage result records directly from
R2 for refresh `ef4ecac6-b82f-4d2a-ae0e-fea7cda2ad19`. Each stage executed once.
This proves remote result persistence, not an observed R2 cache hit. The run
later retried at 07:51 UTC and resumed the durable pending slot operation without
exporting, importing or publishing again; it completed promotion at 07:56 UTC.
That continuation bypassed the build stages. The existing flow-retry integration
test separately checks reuse of completed stage results on a same-run retry.

The `52fe3e7` publisher used for this refresh, and the registered successor, use
`RUN_ID + TASK_SOURCE`, so a fresh flow run
gets new cache keys. Its export/freeze/publication results contain paths on the
builder VM; the import report also depends on that workspace's files. The result
records do not copy the checkpoint or unfinished index into R2. Recovery after
loss of that VM before publication therefore remains a distinct limitation.
After publication, the immutable bundle and manifest are durable in R2 and slot
operations reuse them without export or import. The runtime cutover reused
this refresh's same published artifact. No cache policy or corpus
transfer was changed by this inspection. Root README usage and architecture
claims remain unchanged.

### Managed admission and full-data results

Inactive B completed first admission and cached restart on independent storage
at 10:16:38 UTC. The [prepared plan](candidate-plan.json) and
[journal](candidate-journal-current.json) bind machine `7841e35f99d338`, volume
`vol_4ql13kg2ky19w66r` and the D13 runtime image to the existing full checkpoint.
The manual flow emitted `SlotOperationCompleted`, not the full-refresh event.

The [matched result](matched-performance-attempt-1.json) passed all 39 paired
responses: 26 measured samples per backend after warmup. Baseline/candidate
engine medians were 2.3905/2.3645 ms; sampled p95 values were 3.167/2.615 ms.
This small warm cohort is not a production latency percentile. The
[source](comparison-source.json), [log receipt](comparison-log-receipt.json) and
[cleanup](comparison-cleanup.json) bind the result to the cloud client and confirm
its app was removed.

The [publication-owner result](controller-owner-result.json) passed all 93 checks
with zero skips, including the actual Prefect engine on Linux. The
[package](controller-owner-package.json) includes the required process supervisor
and tested FastAPI/Starlette versions. Earlier qualification packages failed:
one imported an excluded module, another omitted the supervisor, and a fresh
FastAPI resolution rejected Prefect’s server import. No assertion or execution
deadline was relaxed. The [cleanup](controller-owner-cleanup.json) independently
confirms no Machine, volume or temporary app remains.

The [release proof](release-proof.json) binds all six gates to the actual runtime
image, binary, canonical rules, guest and 20 GiB volume. Its immutable R2
[publication](release-publication.json) was read back byte-for-byte before
promotion. The [actual installed hashes](candidate-image-files-actual.json),
[fresh status](candidate-status-release.json) and unchanged
[public Worker](public-worker-current.json) support the binding. This proof
authorizes guarded promotion; it does not claim cutover or the next full refresh
has completed.

### Production cutover

The [promotion flow](promotion-flow.json), `9cd00dc2-b1c7-4f2a-bd00-72b3399c1bed`,
finished as `SlotOperationCompleted` at 10:52:21 UTC. The
[journal](promotion-journal.json) and [route](promotion-route.json) confirm guarded
promotion to B with A retained as rollback. The previous A image still has its
coverage timer and native-provenance dependency; do not prune its objects yet.

The [public checks](public-candidate-check.json) verified the expected B machine,
byte-identical aliases and the expected account under PDS scope. The
[post-promotion index status](post-promotion-index-status.json) confirms the same
full build and binary; [freshness](post-promotion-freshness.json) is healthy and
not stale. This cutover reused the 06:00 full checkpoint and did not renew its
coverage deadline.

The [registered publisher readback](publisher-before.json) still shows the old
immutable pins, concurrency one, `CANCEL_NEW`, and the active 06:00/18:00 UTC
schedule. The unchanged [missed-refresh automation](refresh-automation.json)
expects only `prefect.flow-run.Completed` for this deployment, with its existing
13-hour threshold and notification block. Both manual prepare and promote emitted
`SlotOperationCompleted`, so they do not count as full refreshes. Compatible
registration and the 18:00 normal full refresh remain required.

The root README was compared with this deployed change. Its concise description
of isolated builds and checked cutover remains accurate; detailed lifecycle and
source-log behavior were updated in operations and the refresh runbook instead.

### Post-cutover ranking and cleanup

The [canonical ranking result](ranking-result.json) passed all structural and
Bluesky-parity criteria across the unchanged 11-query corpus. Both providers
returned HTTP 200 for every query. The cloud client used the repository’s actual
benchmark functions with their normal pacing, including debug requests; no
stress test ran. Its [source](ranking-source.json) and
[cleanup](ranking-cleanup.json) identify the exact package and verify removal.
The first client completed the requests but failed to serialize its handle set;
[its cleanup](ranking-attempt-1-cleanup.json) preceded the corrected, locally
serialization-checked capture. Only the replacement result is qualified evidence.

The [manual completion events](manual-completion-events.json) confirm that both
prepare and promote emitted the named success event. The cloud worker’s
[managed-operation cleanup](managed-operations-cleanup.json) records exactly one
VM for each operation, exit 0 and deletion. Independent paginated
[provider inventory](managed-provider-cleanup.json) confirms no owned server,
primary IP or SSH key remains. The serving B and retained A are
managed production resources, not disposable experiments.

### Publisher registration

The [registration readback](publisher-registration.json) confirms the qualified
immutable pins after exact-main CI passed for configuration `090f798`. Only the
command and five release-pin environment fields changed. Schedule, parameters,
pull steps, pool/queue and concurrency were read back unchanged: active
06:00/18:00 UTC, automatic refresh, concurrency one, `CANCEL_NEW`. The source
publisher pin is `0a36b8b`; configuration and runtime source are separately pinned.

The remaining production gate is the normal 18:00 UTC full refresh. Verify its
ordinary `Completed` event, new public build, both aliases and scope, healthy
source, retained rollback and cloud teardown. Do not trigger another full import
merely to finish this observation sooner. Keep `releases/search/` objects until
that refresh removes the older A consumer and a complete consumer audit permits
retirement. The cleanup goal remains active.

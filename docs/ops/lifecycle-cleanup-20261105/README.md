# Lifecycle qualification

Status: fixtures passed and the Prefect API retry fix is live, November 5, 2026.
Inactive-slot preparation was submitted at 09:24 UTC. The candidate runtime is
not serving production. The [cleanup plan](../../plans/lifecycle-cleanup.md)
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

These are fixture results. Full-data matched performance, managed admission,
public cutover and a completed production refresh remain unverified for the new
runtime. No release proof is authorized by this record.

The [source verification](source-verification.json) records commit-by-commit
checks for both repositories, plus 93 actual-reader publisher/controller checks
without skips. All three Typeahead CI workflows passed for `6ab4a59`. Publisher
cleanup is published on both mains at `a4ee1ef`; the registered deployment still
uses `52fe3e7` and the old compatible installer. Its CI is a separate gate before
any release pin changes. The [installed image hashes](image-files.json) identify
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
Serving A remains unchanged. Preparation, matched performance, release proof
and promotion are still pending.

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

The pending publisher fix at
`0a36b8bfe2299a29e87f54c51ed420d341f7a007` preserves the generation's original
`publication_execution_id` when another execution takes over its journal. A
same-run full-refresh retry must still emit ordinary `Completed`; a separate
recovery must not reset the missed-refresh clock. The current manual candidate
continues on its original `2daeb97` pin; this fix is not registered yet.

Before activation, run the [owner fixture runner](run-owner-fixtures.py) in
temporary app `typeahead-controller-owner-20261105`, owned by this cleanup. It
uses the existing runtime image and small committed test sources, two shared
CPUs, 1 GiB RAM, no public service, credentials or volume, and `/tmp` scratch
bounded to 1 GiB for cloud-downloaded dependencies and synthetic data. Bootstrap
and tests have a 600-second bound; the runner requires zero failures and skips.
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
it cannot substitute for the forthcoming real-backend comparison.

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
created by those checks. Positive preparation remains unverified until the full
refresh completes and the normal controller actually qualifies the candidate.

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

The deployed `52fe3e7` publisher uses `RUN_ID + TASK_SOURCE`, so a fresh flow run
gets new cache keys. Its export/freeze/publication results contain paths on the
builder VM; the import report also depends on that workspace's files. The result
records do not copy the checkpoint or unfinished index into R2. Recovery after
loss of that VM before publication therefore remains a distinct limitation.
After publication, the immutable bundle and manifest are durable in R2 and slot
operations reuse them without export or import. The pending runtime candidate
will use this refresh's same published artifact. No cache policy or corpus
transfer was changed by this inspection. Root README usage and architecture
claims remain unchanged.

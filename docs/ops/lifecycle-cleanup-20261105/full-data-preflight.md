# Full-data qualification preflight

Initially recorded November 5, 2026 at 05:57 UTC; updated after the 06:00 refresh
and positive read-only preflight. This does not reserve a slot or authorize promotion.

## Production continuity

The initial [routing](route-before-refresh.json) and
[provider inventory](managed-slots-before-refresh.json) are retained as historical
preflight evidence. The 06:00 refresh, run
`ef4ecac6-b82f-4d2a-ae0e-fea7cda2ad19`, subsequently promoted fresh A, machine
`871327a0347938`, with build `b1793858591-cp-ef4ecac6b8`. It retains B as previous.
Public freshness passed; coverage ends November 6 at 06:03:11 UTC.

The [positive preflight](candidate-preflight.json) verified the serving build,
coverage, unreserved slots, artifact budget and green publisher CI. It created
only a local proposal. Submission is additionally held for the Prefect API's
in-process-retry fix and bounded cloud probe; the refresh exposed two executions
of its retry under one flow-run ID.

Before submission, recheck the public build and route, coverage, current
image/proof binding, both pending markers and next schedule. The controller must
prepare the new runtime on inactive B using the **same immutable artifact as
serving A**, with at least 18 hours of coverage remaining. A and its independent
disk remain untouched. Finish or reconcile the trial before the 18:00 UTC refresh.

## Trial ownership and budgets

Owner: this lifecycle cleanup, through the cloud `typeahead-index` deployment.
The execution pin was updated after the November 5 refresh to
`2daeb97f825c1ad507dccc0020f28ead665f8350`; its main CI passed. The controller
files are unchanged from the cloud-qualified `a4ee1ef`; the flow now names
manual-operation success separately from full-refresh success. Use an explicit
run override, preserving the scheduled
deployment's existing pins until the compatible runtime is qualified and live.
Typeahead source is `7d732639d4d63b1fe0abaeb4b6f07224640d1189`, installer SHA256
`cf893cb984e97453a1807e1de2d52d096664525636829884d734f42ab191f26b`.
The candidate image is
`registry.fly.io/typeahead-tantivy-a@sha256:d13dee4839c3e56ac1f6dd4f0d14cb4514186221b0f5a6d3037cfb92f090473d`.

The candidate's scratch is its own managed 20 GiB `/data` disk, including
`/data/.tantivy-slot` during installation and `/data/tantivy` after admission.
The cloud controller uses its existing disposable `/var/lib/typeahead-indexer`
workspace and four-hour execution bound. Accept a bundle no larger than 8 GiB;
retain the installer's disk reserve and bounded range-resume transfer. Source
polling retains its durable 512 MiB/hour ceiling and 128 MiB reserve. Plan at most
12 GiB of cloud application payload for installation, runtime inputs and four
hours of polling; this is not an ISP or provider billing cap. No corpus bytes
pass through the laptop or HeavyPad.

Resume an interrupted preparation through its existing request and journal;
do not rebuild or allocate another candidate to avoid inspecting a failure.
If qualification fails, keep production on A and reconcile or cancel that exact
owned request through the controller. Cancellation retains storage for recovery;
it is not proof of payload cleanup. Confirm slot ownership and refresh eligibility
after resolving it. A successful candidate becomes a managed serving/rollback
resource rather than an orphaned experiment. The normal controller owns subsequent
retirement; never directly delete a routed machine or volume.

## Qualification criteria

Use the current qualified poller release's unchanged matched-performance bounds:
all paired response bodies identical, candidate warm median no more than 2 ms
above baseline, sampled p95 no more than 5 ms above baseline and at most 100 ms.
The November 2 failed qualification remains a failure; it is not reused as a
passing result. These bounds are for this runtime reliability change, whose reader
binary and retrieval are unchanged, and do not establish a latency improvement or
sustained-load SLO.

Use the existing 13-query cohort, including PDS scope, one warmup and two measured
passes: at most 39 requests per backend, paced at no more than one request per
second to either target. Check exact build, binary, retrieval, machine and scope
acknowledgments. Any error, identity drift or response mismatch fails the run;
preserve the failure instead of discarding a slow cohort. Record all samples.

Managed qualification must also pass actual installation, source catch-up,
moderation, strict readiness and cached restart on the final guest. Preserve the
six empirical release gates and guarded routing CAS. After promotion, verify both
public aliases, scope, backend identity, ranking and freshness. Then register the
compatible provenance-free publisher and verify a full refresh, including its
public build and cleanup, before declaring the lifecycle cleanup complete.

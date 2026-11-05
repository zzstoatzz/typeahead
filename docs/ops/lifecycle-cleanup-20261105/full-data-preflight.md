# Full-data qualification preflight

Recorded November 5, 2026 at 05:57 UTC. This records constraints before any
candidate download; it does not reserve a slot or authorize promotion.

## Production continuity

The [routing readback](route-before-refresh.json) selects B, machine
`812056a97999d8`, and retains A as previous. The
[provider inventory](managed-slots-before-refresh.json) confirms separate
encrypted 20 GiB disks and two shared CPUs / 1 GiB for each slot. B is healthy;
its checkpoint coverage ends November 5 at 18:03:27 UTC. The last full refresh
is `1f545dd4-aa1a-4039-98bd-c8fb1361ce06`; the later prepare/promote runs reused
that artifact and did not renew coverage.

The scheduled 06:00 UTC refresh owns inactive A. Observe its actual run and
completed build before starting a manual candidate. Registration may replace a
scheduled run ID; reconcile that change instead of treating cancellation as a
failed build or starting another refresh. Do not reserve A for this experiment.

After that refresh completes, recheck its public build and route, coverage,
current image/proof binding, both pending markers and the next schedule. Only
then may the existing controller prepare the new runtime on inactive B using
the **same immutable artifact as the refreshed serving A**. Require at least
18 hours of coverage remaining. A and its disk remain untouched. The trial must
finish or be reconciled before the 18:00 UTC refresh needs the inactive slot.

## Trial ownership and budgets

Owner: this lifecycle cleanup, through the cloud `typeahead-index` deployment.
The new controller source is `a4ee1eff1c88e6afb4f2c7d7fec0b119b46161d4`;
its main CI passed. Use an explicit run override, preserving the scheduled
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

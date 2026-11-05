# Lifecycle cleanup

Status: in progress, November 5, 2026 UTC. The goal is to simplify release,
snapshot refresh and recovery without interrupting production or transferring
corpus data through the laptop or HeavyPad.

## Starting point

The [continuous-poller release](../ops/poller-readiness-20261104/) is deployed.
It removes hourly checksum stalls without changing source byte budgets or
reader readiness. Its production observation covered quota renewal and the
former process deadline without a readiness failure.

The replacement runtime is live on B as of November 5 at 10:52 UTC, with
matched full-data, source, scope and public-alias verification. It reports
coverage staleness without stopping and ignores legacy native provenance.
The previous A rollback still has the older behavior. Compatible publisher registration was verified at 11:10 UTC. A normal
scheduled refresh and obsolete-object cleanup remain pending.
The [November 2 retrospective](../retros/2026-11-02-tantivy-slot-expired-again.md)
records the earlier qualification and its failed performance gate. Reuse that
work where valid; do not treat committed source as deployed behavior.

## Scope and verification

1. Qualify the reporting-only coverage runtime together with continuous polling.
   A missed refresh must report stale coverage without killing a healthy reader;
   stale coverage must still prevent admission. Exercise cached restart past
   expiry and retain strict source readiness and PDS scope checks.
2. Remove obsolete native provenance only after the compatible runtime is live.
   Update the cloud publisher and installer pins together, then verify a normal
   scheduled refresh through public results. Do not delete retained objects until
   all live consumers have been checked.
3. Verify refresh and recovery with an unavailable old backend, interrupted
   startup and durable resume. Use existing controller paths and real isolated
   I/O tests; retain route ownership, conditional writes and independent storage.
4. Keep missed-refresh detection tied to full refreshes. The live automation
   currently watches every `prefect.flow-run.Completed` for `typeahead-index`,
   including manual prepare/promote and publication-only runs. Those operations
   do not renew checkpoint coverage and must not reset the 13-hour retry clock.
   Verify the new publisher's success events and automation together before
   completion; preserve the existing retry cadence and notification destination.
5. Verify retry ownership through the Prefect API. The November 5 refresh
   exposed a missing upstream worker-poll filter: `in_process` retries were
   returned as new work, launching two executions of one flow-run ID despite
   deployment concurrency one. The server fix and HTTP/backend checks are
   [recorded in its incident](https://tangled.org/zzstoatzz.io/prefect-server/tree/main/docs/incidents/2026-11-05-in-process-retry-resubmission.md).
   Verify the API rollout and a bounded cloud retry probe before another slot
   operation. Concurrency limits and production timeouts stay unchanged.

Before reserving the inactive slot, check the actual last full refresh, coverage
deadline and next scheduled run. The scheduled publisher keeps ownership until
an explicit safe trial window is established. Never modify the serving slot for
qualification. Record the candidate's owner, scratch root, disk budget, transfer
budget and teardown before any corpus-scale work.

Local checks use small synthetic fixtures. Corpus builds, downloads and load
generation run in cloud infrastructure. Local observations are bounded metadata
and low-rate health requests; HeavyPad remains the control plane. Retire trial
processes and payloads after verification, preserving small evidence records.

Update affected operational docs with each change and dated deployment readback.
The README stays a concise entry point: change it only when its architecture or
usage claims change. Completion requires verified production behavior, a
successful full refresh, recovery evidence and confirmed experiment cleanup.

## Changes under qualification

The deployed slot runtime sends source stdout and stderr to the supervisor's log
stream, collected by Fly. It no longer writes or rotates `source.log` on the
serving volume. Rotation depended on process exit and therefore did not bound a
continuous poller's log. This change is live on B. Use `fly logs -a APP`
for source diagnostics after that runtime is released.

The [November 5 cloud fixture run](../ops/lifecycle-cleanup-20261105/) passed
all 36 tests and removed its disposable compute. It verifies the combined
runtime and provenance-free checkpoint helpers on synthetic data, including
restart beyond the coverage deadline. Full-data qualification and a verified
production refresh remain required.

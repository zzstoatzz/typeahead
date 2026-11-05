# Lifecycle cleanup

Status: in progress, November 5, 2026 UTC. The goal is to simplify release,
snapshot refresh and recovery without interrupting production or transferring
corpus data through the laptop or HeavyPad.

## Starting point

The [continuous-poller release](../ops/poller-readiness-20261104/) is deployed.
It removes hourly checksum stalls without changing source byte budgets or
reader readiness. Its production observation covered quota renewal and the
former process deadline without a readiness failure.

The serving image still stops before its checkpoint coverage expires and
requires legacy native-manifest provenance. Main contains replacements for
both behaviors, but they have not passed the complete deployment qualification.
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

The local slot runtime sends source stdout and stderr to the supervisor's log
stream, collected by Fly. It no longer writes or rotates `source.log` on the
serving volume. Rotation depended on process exit and therefore did not bound a
continuous poller's log. This change is not deployed yet. Use `fly logs -a APP`
for source diagnostics after that runtime is released.

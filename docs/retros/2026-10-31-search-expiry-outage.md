# Search outage after snapshot coverage expired

Status: production restored on B at 16:08:50 on October 31, 2026; automatic
Bluesky fallback is deployed. Full refresh verification is still running.
Times below are UTC.

## What happened

Production Tantivy A stopped at 07:47:58. Its admission recorded a coverage
deadline of 07:48:56; the slot deliberately stops one minute before that deadline.
The logged `ValueError` and exit time match this shutdown path. Fly retried three
times, then left the machine stopped. Public search returned 503.

The scheduled publisher attempt at 05:59:59 failed before executing because
Hetzner rejected its VM allocation in `fsn1` with HTTP 412 `resource_unavailable`.
A healthy, tested PDS-join candidate B existed, but routing still pointed to A.
There was no automatic Bluesky failover. The user reported the outage.

## Recovery mistakes

I started a normal candidate promotion before using the documented emergency
Bluesky switch. That unnecessarily prolonged the outage. The promotion then
failed because its shared mutation guard required the *old* serving backend to
be healthy. The user had to point out the existing `EMERGENCY_PROXY_BSKY` switch.
Enabling it immediately restored unscoped searches on both public aliases.

The preceding experiment was declared complete with production unchanged, but
that was insufficient: production had an expiring coverage deadline and no
verified successful next refresh. Candidate readiness and production continuity
are separate requirements. A candidate occupying the inactive slot also needs
an explicit refresh/expiry plan; its existence is not protection for live traffic.
We have not established that slot occupancy caused this particular allocation
failure; the recorded failure was cloud capacity.

## Changes

- Public unscoped search falls back to Bluesky on backend failure, timeout or
  invalid response. This runs in the public Worker, independently of Prefect,
  the cloud builder, and operator availability.
- Fallback responses identify `X-Typeahead-Engine: bluesky` and
  `X-Typeahead-Failover: automatic` (or `manual` for the operator switch), emit
  structured outcome logs, and are not cached. Each uncached request tries the
  primary again, so recovery does not require a scheduler to undo a flag.
- PDS filters/preferences continue to return `503 PdsScopeUnavailable` during
  fallback. Bluesky cannot promise our PDS scope contract. Invalid queries still
  return 400; valid empty primary results are not treated as failures. If both
  providers fail, return 503 rather than a misleading successful empty result.
- Promotion keeps route ownership, conditional writes, release proof and fresh
  candidate checks, but does not require a dead source backend to recover.
  A retry may resume only its own unchanged mutation lease.
- The scheduled publisher pins the joined mapping, reader, source and release
  proof, and uses `hel1`, where the candidate controller successfully allocated.
  A completed subsequent refresh is required before claiming refresh reliability.
  The deployment allows one concurrent run and cancels overlapping submissions;
  the live limit and active slot were read back after configuration.

## Before every serving experiment

1. Read this retrospective and the current operations record. Enumerate serving,
   candidate and rollback machines, their storage, routing and refresh owners.
2. Check production readiness, source lag, **coverage expiry time**, and the last
   successful scheduled refresh. A schedule entry is not evidence of execution.
3. Ensure the next refresh can finish before expiry, including provisioning,
   snapshot build, transfer, startup and verification. State who owns the inactive
   slot and how the scheduled publisher behaves while it is reserved.
4. Verify automatic failover with a real runtime failure test. Keep the manual
   command immediately available. Never use Turso full-corpus queries as an outage
   fallback: that can damage ingestion as well as search.
5. Exercise recovery with the old backend unavailable, not just a healthy-to-healthy
   swap. Verify both public aliases, scope behavior, route identity and recovery.
6. After the experiment, check production continuity again. Do not substitute
   “production unchanged” for a valid lifetime and a working refresh path.

## First action during a search outage

Restore ordinary searches before diagnosing the builder or deploying new code:

```sh
printf '1' | bunx wrangler secret put EMERGENCY_PROXY_BSKY --config wrangler.jsonc
```

Verify real results through both public aliases. PDS scopes are intentionally
unavailable in this mode. Restore a qualified backend through the conditional
routing path. Remove the manual switch only with automatic fallback deployed,
then verify public backend identity and scoped results. Do not disable expiry
checks or fake freshness to bring an old snapshot back online.

## Evidence and limits

[Recovery evidence](../ops/production-recovery-20261031/) records public checks,
flow requests and publisher pins. [Candidate qualification](../ops/archive-pds-candidate-20261030/)
records scope/update/restart checks and coverage measurements.

Automatic fallback is degraded service: results follow Bluesky's coverage and
ranking, and depend on Bluesky availability. A timed-out primary adds up to its
three-second request budget before the five-second fallback budget. It cannot
rescue a failure of the public Worker/platform itself. No request-triggered design
can establish availability when no requests arrive; it does restore the first
failed search without waiting for a periodic monitor.

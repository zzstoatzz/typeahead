# Search cutover execution

Nate approved the prepared cutover: “yes go! watch as it goes”. On October 25,
2026, public Typeahead switched to `https://typeahead-startup-20261024.fly.dev`
after confirming both expected Worker versions, candidate freshness and rollback
backend readiness. No original backend restart, resize, volume mutation, shared
snapshot publication, or whole public Worker source deployment occurred.

The transport allowlist deployment is `8de9d8f4-ad1a-4130-9e24-1ba5c9b7a85b`.
The public binding-only deployment is `e7497249-a134-451b-8544-afda9c3bfe44`.
The serving image remains the qualified immutable digest `9822d2b735cbfd5cda65b1bc4b8c6f95fcfd74e3d109c78e22b9a5b596247573`,
build `b1792896197-ebe5-px05`, two shared CPUs / 1 GiB.

## Observed acceptance

Five rounds over approximately two minutes passed freshness HTTP 200, both public
lexicon aliases, first-ranked exact handle, hostname and URL PDS forms, empty
unknown-PDS results, and expected excluded-PDS rejection. Direct backend PDS
acknowledgment passed. Replica lag advanced through a sync cycle: 278, 5, 36,
67, and 98 seconds. The slowest sampled public search request was 272.78 ms.
These are rollout probes, not a new load test or a claim about every cold query.
The separately measured clean process launch-to-attach time is 71.324 ms.

The real Evergreen inventory/status validator now reports Typeahead search
freshness HTTP 200 and ingester HTTP 200. The Fleet User-Agent also receives
freshness HTTP 200. Alerts were not muted or reconfigured. This verifies the
upstream check used by Fleet; it does not claim a later scheduled Fleet sweep
has already run or that old Discord messages disappear.

## Cleanup and retained resources

Removed 11,026,783,033 bytes of inactive test generation, pre-compaction overlay,
and preflight scratch files after confirming no process had them open. Removed
five obsolete private R2 object keys; no candidate-owned multipart uploads remain.
All three cloud protection helper units are inactive with zero MainPID; their two
helper scripts were removed, and the full artifact readback receipt is retained.
Qualification JSON/logs remain in the repository and small remote evidence folders.
No benchmark or download process remains on the serving machine.

The new machine/volume and private protected snapshot channel are serving
resources. The original `typeahead-search-compact` machine `86e502fe55e4e8` and
volume `vol_vxmqwnpnlewdgzj4` remain intact and running as the agreed routing
rollback path. Do not retire them without agreement. Rollback changes only
`SEARCH_BACKEND_URL` to `https://typeahead-search-compact.fly.dev` using the
[binding-only procedure](search-cutover-20261025.md). The old snapshot remains
stale; rollback restores availability rather than freshness.

The private snapshot channel remains frozen and the bulk publisher remains
paused. Future snapshots require isolated offline preparation until serving-time
promotion is qualified. Refresh before October 29, 2026 at 02:43 UTC, when this
snapshot reaches the existing four-day freshness limit. No refresh schedule or
alert threshold was changed as part of this cutover.

## Evidence

- [Deployment identities](search-cutover-deployments-20261025.json)
- [Final resource inventory](search-resources-aftercutover-20261025.json)
- [Five public verification rounds](../evals/startup-cutover-live-20261025.json)
- [Evergreen results](../evals/startup-evergreen-20261025.json)
- [Fleet HTTP probe](../evals/startup-fleet-probe-20261025.json)
- [Serving storage cleanup](../evals/startup-serving-cleanup-20261025.json)
- [Private R2 cleanup](../evals/startup-private-cleanup-20261025.json)
- [Cloud helper cleanup](../evals/startup-cloud-helper-cleanup-20261025.json)

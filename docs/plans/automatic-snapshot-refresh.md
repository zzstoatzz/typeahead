# Automatic snapshot refresh

The cloud Prefect `typeahead-index` deployment in
[my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server) owns builds
and managed Tantivy A/B rollout. Its deployment definitions and
[slot lifecycle](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/tantivy-slot-lifecycle.md)
are authoritative for controller parameters and image pins.

## Normal lifecycle

1. Export a consistent Turso checkpoint and join the pinned archive PDS mapping.
2. Build and validate the index, then publish immutable artifacts to R2.
3. Install on the inactive A/B slot with its own disk; catch up source changes.
4. Qualify source coverage, search behavior, final allocation and cached restart.
5. Promote by conditional routing update and verify public results. Preserve the
   previous Tantivy backend for rollback.

The schedule is 06:00 and 18:00 UTC. Checkpoint coverage lasts 24 hours; the slot
stops before expiry. The cadence is a coverage-renewal policy, not a Tantivy
requirement. Full remote reconciliation is not yet qualified to replace it.
The deployed controller has a four-hour outer limit and one-hour readiness
phases. These are failure bounds, not expected build times.

The [lifecycle cleanup](lifecycle-cleanup.md) tracks the remaining runtime and
release work. Its proposed behavior is not a description of production.

## Safety and recovery

Follow the [production continuity preflight](../retros/2026-10-31-search-expiry-outage.md#before-every-serving-experiment).
A manual candidate occupying the inactive slot can fence automatic refresh.
Check the next run against the serving coverage deadline, including build and
installation time. Bulk work runs in cloud infrastructure; HeavyPad hosts the
Prefect control plane, not corpus processing.

Use `just refresh-search` to submit the configured deployment. It does not deploy
the local checkout. A failed rollout with retained artifacts can use the existing
`resume_rollout:true` path; inspect its journal and ownership before retrying.
Never rebuild the serving slot, override a newer route, or fake admission freshness.
The fixed slot image still requires each manifest's native anchor manifest in R2;
see [operations](../operations.md#deploy-and-verify) before pruning release
objects. Retired apps may still host pinned registry images; do not delete
app registrations as a substitute for retiring unused machines and volumes.

See [recovery evidence](../ops/production-recovery-20261031/) for the last verified
full lifecycle. The [old native refresh procedure](../archive/native-snapshot-refresh.md)
describes the retired native engine and is history only.

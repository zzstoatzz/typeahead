# Operations

Production uses the public Cloudflare Worker, search transport and managed
Tantivy A/B slots. Start with the [architecture](architecture.md) for data flow
and the [refresh runbook](plans/automatic-snapshot-refresh.md) for deployments.
Historical native machine IDs and schedules are not operational instructions.

## Before changing infrastructure

Read the [outage retrospective and preflight](retros/2026-10-31-search-expiry-outage.md).
Check the live route, serving readiness, coverage deadline, last completed refresh
and inactive-slot ownership. An enabled schedule does not prove a refresh succeeded.
Never prepare a snapshot on the serving machine or its disk.

```sh
curl -fsS https://typeahead-search-transport.n8-3e9.workers.dev/route
just index-status
```

Use the route's explicit origin for `/ready` and `/index/status` diagnostics.
Confirm the expected build and source coverage; do not treat a recent polling
watermark as full reconciliation. Smoke and benchmark commands can default to
production: use isolated targets for load tests.

## Outages

Unscoped searches automatically fall back to Bluesky on backend failure.
PDS-scoped requests fail explicitly because Bluesky cannot honor that contract.
The manual `EMERGENCY_PROXY_BSKY` override and recovery procedure are documented
in the [outage runbook](retros/2026-10-31-search-expiry-outage.md#first-action-during-a-search-outage).
Restore service before investigating the builder. Never bypass coverage expiry.

## Deploy and verify

`just deploy-worker` deploys the public Worker and runs smoke checks.
`just deploy-ingest` deploys the ingester. Backend publication belongs to the
cloud Prefect controller, not a direct Fly deploy. After promotion, check the
build, both public search aliases, PDS scope, ranking and live-update progress.

The controller needs the retained native rollback anchor. Do not remove it merely
because it is not serving traffic. Retire unused trials by exact machine and
volume identity after checking routing, refresh ownership and preview bindings.
Keep small evidence receipts, not unused corpus copies.

## Edge configuration

Public `/xrpc/` requests must work for non-browser API clients. Preserve the
Cloudflare Browser Integrity Check exception and let origins control cache TTLs
with Respect Existing Headers. Background errors and all search responses remain
in operational counts; `X-Client: smoke-testing` is excluded from app/adoption counts.

## Evidence and history

- [October 31 recovery](ops/production-recovery-20261031/): verified full refresh,
  automatic fallback and publisher ownership.
- [Trial retirement](ops/test-resource-retirement-20261031/): removed resources,
  retained dependencies and post-removal search checks.
- [Historical operations](archive/native-operations-through-20261031.md): dated
  incidents, previous native deployments and old tuning experiments. Commands and
  machine inventories there must not be used as current deployment instructions.

# Operations

Production uses the public Cloudflare Worker, search transport and managed
Tantivy A/B slots. Start with the [architecture](architecture.md) for data flow
and the [refresh runbook](plans/automatic-snapshot-refresh.md) for deployments.
The retired native engine's machine IDs and schedules are history, not
operational instructions.

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

`just deploy-worker` lints and type-checks before deploying the public Worker,
then runs smoke checks. `just deploy-search-transport` uses the same gates.
`just deploy-worker-only` skips smoke checks but still requires lint to pass.
`just deploy-ingest` deploys the ingester. Backend publication belongs to the
cloud Prefect controller, not a direct Fly deploy. After promotion, check the
build, both public search aliases, PDS scope, ranking and live-update progress.

Rollback is to the previous Tantivy slot; there is no native backend. The fixed
slot image (Typeahead `c476fc5`) still validates each Tantivy manifest's
`rollback_native_anchor` and fetches that anchor's
`releases/search/<build>/manifest.json` from R2 by SHA-256 during installation,
so keep every native manifest object an admitted Tantivy manifest references
while any slot runs that image. The current slot runtime source no longer fetches or
validates native manifests and ignores `rollback_native_anchor`,
`legacy_build_id` and `source_manifest_key`; no image built from it is deployed
yet. Retire unused trials by exact machine and
volume identity after checking routing, refresh ownership and preview bindings.
Keep small evidence receipts, not unused corpus copies.

## Edge configuration

Public `/xrpc/` requests must work for non-browser API clients. Preserve the
Cloudflare Browser Integrity Check exception and let origins control cache TTLs
with Respect Existing Headers. Background errors and all search responses remain
in operational counts; `X-Client: smoke-testing` is excluded from app/adoption counts.

## Search exclusion counts

`/stats` shows two figures from the daily `recordExclusions` cron step
(`src/exclusions.ts`, KV `search_exclusions`), logged as `exclusions_recorded`:

- **hidden by policy**: accounts the moderation rule excludes. The stored
  `actors.hidden` flag only nominates candidates. Each one is re-evaluated with
  `resolveHidden` — the rule Tantivy applies at query time — so flags set under
  older rules are counted as `stale_hidden_flags`, not exclusions. The quality
  walk selects `hidden = 0`, so the mod refresh never revisits a hidden row and
  those flags do not clear on their own.
- **invalid identity**: `handle.invalid` plus quarantined handle claims. Nearly
  all of the quarantined count is one PLC spam wave: DIDs whose only operation
  claims the `trump.com` handles in `src/identity-quarantine.json`.

Neither figure measures moderation across the network. Since 2026-11-01 the
ingester records new Bluesky moderation labels from the labeler's stream (see
`labels` in the ingester's `/health`); labels set before then still arrive only
when the refresh cron revisits an account, until the one-time reconciliation
runs. Host suspensions and takedowns are deleted from the index rather than
hidden. The quarantined count walks one
covering-index range of about 1.76M entries (about 4 s), which is why it runs
daily, not hourly. Blocked-domain subdomains are counted only once a domain-block
backfill has flagged them.

## Evidence and history

- [October 31 recovery](ops/production-recovery-20261031/): verified full refresh,
  automatic fallback and publisher ownership.
- [Trial retirement](ops/test-resource-retirement-20261031/): removed resources,
  retained dependencies and post-removal search checks.
- [Historical operations](archive/native-operations-through-20261031.md): dated
  incidents, previous native deployments and old tuning experiments. Commands and
  machine inventories there must not be used as current deployment instructions.

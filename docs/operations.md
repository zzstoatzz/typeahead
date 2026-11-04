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
Restore service before investigating the builder.

The fixed slot image (`c476fc5`) stops itself 60 seconds before its checkpoint
coverage deadline (last completed full round, or the checkpoint cut, plus
86,400 seconds). The current slot runtime source never stops on coverage age:
a running or restarted slot keeps serving and records `coverage`
(`coverage_anchor`, `coverage_deadline`, `coverage_age_s`, `coverage_stale`,
`checked_at`, `basis`) in `/data/tantivy/installation.json` every 5 seconds,
and logs a `tantivy_slot_coverage` event when staleness changes. Admission
(`validate_runtime`, used by qualification and retirement) still refuses stale
coverage, so a stale slot is never promoted. No image built from this source
is deployed yet. The Worker's `/health/freshness` reads the serving backend's
`/index/status` and reports `coverage_age_s` and `coverage_deadline` for a
checkpoint-sourced generation; at 86,400 seconds or more it sets `stale: true`
(HTTP 503, change-only webhook alert) while `ok` and `probe_ok` stay true.

### Missed-refresh alerts

Refreshes run at 06:00 and 18:00 UTC against a 24-hour coverage window, so one
missed run uses up the margin. Each signal below fires once, on change:

1. **Builder capacity:** a Hetzner capacity rejection retries every location and
   fallback server type for up to 30 minutes before the run crashes.
2. **13 hours with no completed `typeahead-index` run:** the Prefect automation
   `typeahead refresh missing -> retry + discord` starts the automatic refresh
   and posts to discord-alerts.
3. **15 hours of coverage age:** the Worker posts "refresh missed — act before
   the slot goes stale" with the hours left. Search is still serving; find out
   why the retry did not land.
4. **24 hours:** "serving is stale". The next automatic refresh replaces a stale
   or dead slot; use `EMERGENCY_PROXY_BSKY` only if search itself fails.

## Deploy and verify

`just deploy-worker` lints and type-checks before deploying the public Worker,
then runs smoke checks. `just deploy-search-transport` uses the same gates.
`just deploy-worker-only` skips smoke checks but still requires lint to pass.
`just deploy-ingest` deploys the ingester. Backend publication belongs to the
cloud Prefect controller, not a direct Fly deploy. After promotion, check the
build, both public search aliases, PDS scope, ranking and live-update progress.

### Request latency

Every public Worker response adds `worker;dur=...` to `Server-Timing`, measured
from fetch entry until response headers are ready. Requests that check the rate
limiter also add `rate_limit;dur=...`, including denied checks. These timings are
added after cache handling, so cache hits report the current request's timing.
The `worker_request_timing` log records the same stages with endpoint, method,
status, ingress colo and Cloudflare ray ID; uncaught errors also produce a log.
Workers traces are enabled alongside the existing full-sampling logs.
`worker_request_start` marks fetch entry. Compare Cloudflare's native log
timestamps for this event and `worker_request_timing` with the invocation's
`eventTimestamp`: the interval before the first log exposes platform dispatch
time that the in-code clock cannot measure. An invocation's total wall time
also includes background work, so it is not response latency.

Search's existing `search`, `cache` and `origin` timings remain nested within
`worker`. The `/stats` latency counters measure the search handler, which starts
after the rate-limit check; they are not full Worker or client latency. Compare
client time after TLS against `worker`, then `worker` against `rate_limit` and
`search`, to locate delays. Worker clocks advance on I/O, so these are not CPU
profiles. They exclude time before fetch entry, response-body streaming,
network delivery and background `waitUntil` bookkeeping.

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

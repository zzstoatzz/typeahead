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

The serving runtime no longer stops on checkpoint coverage age (live November 5,
2026 at 10:52 UTC; [qualification and cutover](ops/lifecycle-cleanup-20261105/)).
A running or restarted slot keeps serving and records `coverage`
(`coverage_anchor`, `coverage_deadline`, `coverage_age_s`, `coverage_stale`,
`checked_at`, `basis`) in `/data/tantivy/installation.json` every 5 seconds,
and logs `tantivy_slot_coverage` when staleness changes. Admission still refuses
stale coverage, so a stale candidate cannot be promoted. Source and moderation
readiness retain their existing strict limits.

The Worker's `/health/freshness` reports checkpoint `coverage_age_s` and
`coverage_deadline`. Beyond 86,400 seconds it reports `stale: true` (HTTP 503,
change-only alert), while `ok` and `probe_ok` remain true if search works. This
alert still requires a refresh. Source diagnostics use `fly logs -a APP`; the
continuous source process no longer accumulates `source.log` on its volume.

The previous A rollback still uses the older expiry runtime until the next full
refresh replaces it. Keep retained native provenance objects while that consumer
exists. The compatible publisher was registered at 11:10 UTC with unchanged schedule
and concurrency. Verify its normal scheduled refresh in the release record
before pruning.

### Missed-refresh alerts

The serving source poller uses continuous mode with a durable hourly byte
budget, live November 5, 2026 at 04:26 UTC. The older image restarted the
poller hourly and rechecked the entire immutable PDS mapping each time, pausing
polling for about 88 seconds. The [release record](ops/poller-readiness-20261104/)
contains the observed readiness failures, guarded cutover and hourly-boundary
verification. See the
[source adapter](../services/tantivy-input/README.md) for the unchanged work bounds.

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
then runs API and image smoke checks. `just deploy-search-transport` also lints,
type-checks and runs the API smoke suite.
`just deploy-worker-only` skips smoke checks but still requires lint to pass.
`just deploy-ingest` deploys the ingester. Backend publication belongs to the
cloud Prefect controller, not a direct Fly deploy. After promotion, check the
build, both public search aliases, PDS scope, ranking and live-update progress.

The private `typeahead-image-renderer` Worker owns PNG preview cards and icons.
Deploy it with `just deploy-image-renderer`, which checks fresh images through
the public service binding. It has the shared stats KV binding, no public
route, and no Turso or admin secrets. Deploy it before introducing or changing its public
`IMAGE_RENDERER` binding; on initial installation, qualify the private service
through a temporary verifier Worker before switching the public Worker.
Renderer and public Worker versions can be rolled back independently while
their image URL contract stays compatible. Keep the renderer deployed while
any public version refers to it.

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
Do not infer handler return by adding `worker_ms` to `eventTimestamp`: event
receipt can precede fetch entry by hundreds of milliseconds. Use the native
entry and completion log timestamps. The [November 4 activation evaluation](evals/worker-startup-cause-20261104/result.json)
isolates pre-handler delay in the full bundle and the image renderer's
contribution; its renderer-removed and lazy variants were temporary trials.
The [first-request follow-up](evals/first-request-latency-20261104/result.json)
compares minification, transport placement and late authority reads, then
records the deployed versions and European probes. Its induced-delay routing
qualification is separate from the natural production measurements; neither
sample maxima nor a local startup profile establish a latency guarantee.

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

## Telemetry

Each signal has one home:

| question | where to look |
| --- | --- |
| is search serving fresh results? | `/health/freshness` on the public Worker, and `just index-status` |
| is the ingester writing? | `https://typeahead-ingester.fly.dev/health`, and `ingest.heartbeat` in Logfire |
| what did the Worker do with a request? | Workers Logs for `typeahead` (`worker_request_timing`, `ingest_done`, `cron_done`) |
| machine CPU, memory, disk | the [Fly dashboard](../ops/health/README.md) |

Logfire's `typeahead` project receives records from the ingester only. The
Worker and the Tantivy slots do not export there.

The ingester emits one `ingest.heartbeat` span a minute, after a flush that
succeeded, so a gap means writes are failing or the process is down. Its
attributes are `total_ingested`, `total_deleted`, `queued_ingest`,
`queued_deletes`, `failed_flushes`, `backpressure_count`, `backpressure_active`,
`relay_cursor` and `rss_kb`; the counters restart at zero with the process. The
`ingester heartbeat missing` alert fires after 15 minutes without one.

Warnings and errors from every ingester module ship as log records. Two kinds
of line stay on stderr (`fly logs -a typeahead-ingester`) and are not sent:
the per-flush progress and retry lines, and zat's connection progress. Both
are decided in `services/src/log_route.zig`. Before 2026-11-10 they were about
70% of the project's records and repeated what the heartbeat carries.

Worker writes from the ingester wait up to 30 seconds for a response. A
`worker write transport failed: ResponseHeadTimeout` record means a write
outlasted that; check `database_ms` on `ingest_done` in Workers Logs and what
else is writing to Turso (the 06:00 and 18:00 UTC index export, the nightly
enrichment) before changing the ingester.

## Edge configuration

Public `/xrpc/` requests must work for non-browser API clients. Preserve the
Cloudflare Browser Integrity Check exception and let origins control cache TTLs
with Respect Existing Headers. Background errors and all search responses remain
in operational counts; `X-Client: smoke-testing` is excluded from app/adoption counts.

## Statistics dual write to D1

Request and traffic statistics are moving from Turso to the `typeahead-stats`
D1 database ([plan](plans/stats-off-turso.md), issue 34). Steps 1 and 2 of that
plan were implemented on 2026-11-10 and had not been deployed when this was
written; record the deploy and `T0` (the first 5-minute bucket that begins
after it) here.

- The public Worker binds the database as `STATS_DB`. Its schema is the nine
  statistics tables as production Turso defines them, in `migrations/stats-d1`.
  Apply migrations before deploying a Worker version that needs them:
  `bunx wrangler d1 migrations apply typeahead-stats --remote`.
- While `STATS_DUAL_WRITE` is `"1"` (the default in `wrangler.jsonc`), every
  statistics write goes to Turso and to D1: `request_buckets`, `metrics`,
  `traffic_sources`, `traffic_buckets`, `actor_deltas`, `counters.actors_total`
  and the hourly `snapshots` row. On the request path each store's write is its
  own `waitUntil` with its own swallowed failure, logged as
  `stats_write_failed` with `store` set to `turso` or `d1`.
- Turso is still the only source for `/stats`, `/source-icon`, the KV
  materialization and the 90-day rollup. Nothing reads D1, and D1 runs no
  rollup or prune yet: its bucket tables only accumulate until the plan's
  step 4.
- To stop the D1 writes without a code change, deploy with
  `--var STATS_DUAL_WRITE:0`, or set the variable to `"0"` in `wrangler.jsonc`.
  A later plain deploy restores the value in `wrangler.jsonc`.
- To check that rows are landing (read-only):
  `bunx wrangler d1 execute typeahead-stats --remote --command "SELECT datetime(bucket * 300, 'unixepoch') AS bucket_utc, sum(hits) AS answers FROM request_buckets GROUP BY bucket ORDER BY bucket DESC LIMIT 6"`.

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

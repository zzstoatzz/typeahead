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

### Store role

`MODE=store` (not deployed anywhere as of 2026-11-10) reports in the same
places under its own names. `/health` answers `"role":"store"` with the
store's numbers: the stored relay, labeler, PLC and bridge cursors, the change
log's head, floor and each consumer's position, queue depths, table count and
level sizes, bytes on disk, resident metadata, stall totals, last commit and
last compaction, actor deltas since start, and the index bridge's position and
lag when it is configured. The numbers under `store` and `queues` are a
snapshot the owner refreshes once a minute (`store.taken_at`). For 30 s after
a process has opened the store the owner counts nothing: `taken_at` is 0 and
the queue depths, `handle_repair.remaining` and the bytes on disk read zero,
while the cursors, the walks, the repair's position and the table statistics
are those read at the open. `/store/*` answers 503 for those 30 s and until
its handle is open, and `corpus` stays empty until the first count, 300 s in.

`enrichment` is the profile refresh loop: the pacing in force, entries leased,
completed, retried and not found, requests failed and rate-limited, the last
success, the pause in force (`backoff_until`), and for each of the two walks
its hourly rate, passes, position and the period that rate gives for the pool
the last corpus scan counted. `enrichment.handles` counts the profiles whose
handle differed from the stored one (`disagreed`), none of which is written:
`queued` for identity resolution, `pending` because the actor was queued
already, `settled` because its identity was resolved within
`settle_seconds`, and `deferred` because `backlog_limit` identity entries
were due. A `deferred` count that keeps rising beside a high
`queues.identity_due` means the resolvers are behind. `queues.refresh_due` is what waits now and
`queues.refresh_leased` what a fetch holds. `corpus` is the last count of the
actor range (live, with handles, with avatars, hidden, and `total` as the
statistics page defines it) with the scan's `duration_ms`. Mechanism and
rates: [the embedded-store plan](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md#enrichment).

`control` is the loop that pulls moderation policy and indexing requests from
the Worker ([slot control](slot-control.md#watching-it)): whether it is `on`
or `misconfigured`, the policy revision the store holds and when the Worker
last confirmed it, the last indexing request taken, the failures by kind, and
`age_seconds`, the time since both endpoints last answered. Alert when the
heartbeat's `control_age_seconds` passes 300 or `control_misconfigured` is
true: search is unaffected, but a policy change made at the Worker is not in
force on that slot until it falls. The Worker's `GET /health/control` says
whether the control database itself answers. No slot runs the loop as of
2026-11-12.

`handle_repair` is the pass that sends every `handle.invalid` record back
through identity resolution, once in the life of a store: when it started and
finished, holders `examined` and `queued`, how many are `remaining`, and the
last DID it looked at. It is done when `finished_at` is set. `HANDLE_REPAIR=0`
stops it; its position is kept, so removing the variable resumes it.

It emits one `store.heartbeat` span a minute, from the owner after that
refresh, so a gap means the owner is not turning or the process is down. The
per-commit progress line stays on stderr through `services/src/log_route.zig`,
like the ingester's per-flush line; warnings and errors ship as log records.
Configuration is listed in `services/src/store_process/config.zig`.

## Edge configuration

Public `/xrpc/` requests must work for non-browser API clients. Preserve the
Cloudflare Browser Integrity Check exception and let origins control cache TTLs
with Respect Existing Headers. Background errors and all search responses remain
in operational counts; `X-Client: smoke-testing` is excluded from app/adoption counts.

## Statistics dual write to D1

Request and traffic statistics are moving from Turso to the `typeahead-stats`
D1 database ([plan](plans/stats-off-turso.md), issue 34). Steps 1 and 2 of that
plan went live on 2026-11-10: the schema was applied and the Worker deployed at
19:19:01 UTC. `T0`, the first 5-minute bucket that begins after the deploy, is
2026-11-10 19:20:00 UTC (bucket 5981128). The four closed buckets from `T0`
to 19:35 held identical totals in both stores when checked (407, 537, 394,
489 answers).

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
- Turso is the source for `/stats`, `/source-icon`, the KV materialization and
  the 90-day rollup while `STATS_READ_SOURCE` is `"turso"` (the default in
  `wrangler.jsonc`). Until it is `"d1"` nothing serves from D1 and D1 runs no
  rollup or prune. The cutover is the next section.
- To stop the D1 writes without a code change, deploy with
  `--var STATS_DUAL_WRITE:0`, or set the variable to `"0"` in `wrangler.jsonc`.
  A later plain deploy restores the value in `wrangler.jsonc`.
- To check that rows are landing (read-only):
  `bunx wrangler d1 execute typeahead-stats --remote --command "SELECT datetime(bucket * 300, 'unixepoch') AS bucket_utc, sum(hits) AS answers FROM request_buckets GROUP BY bucket ORDER BY bucket DESC LIMIT 6"`.

## Statistics read cutover to D1

Steps 3 to 6 of the [plan](plans/stats-off-turso.md). Status on 2026-11-12:
the code is on the `stats-d1-read` branch, tested locally, not deployed; no
backfill has run and production reads Turso.

Measured on 2026-11-12 08:25 UTC with read-only queries. Turso held about
294,000 statistics rows; D1 held only what it had counted since `T0`:

| Table | Turso rows | D1 rows | Turso range |
| --- | --- | --- | --- |
| `request_buckets` | 141,079 | 3,999 | from 2026-08-23 |
| `actor_deltas` | 62,652 | 449 | from 2026-03-19 |
| `traffic_buckets` | 49,813 | 1,239 | from 2026-08-14 |
| `metrics` | 35,153 | 449 | from 2026-03-17 |
| `snapshots` | 4,847 | 37 | from 2026-03-17 |
| `traffic_daily` | 659 | 0 | 2026-07-24 to 2026-08-14 |
| `request_daily` | 0 | 0 | no bucket is 90 days old yet |
| `traffic_sources` | 410 (1,169,465 hits) | 66 | |
| `counters` | `actors_total` 13,162,119, and `label_stream_seq` | `actors_total` 30,161 | |

Over the 445 closed buckets from `T0` to that time the two stores had counted
independently: 183,963 answers in Turso and 183,944 in D1, 29,690 and 29,692
attributed searches, 29,747 searches and 928 cache hits in both, and actor
deltas summing to 29,948 and 29,936. Each store drops a few writes the other
keeps.

### What the three pieces do

- **Backfill** (`POST /admin/stats-backfill`, `src/stats-migration/backfill.ts`, driven
  by `just stats-backfill`). The rows go from Turso to D1 inside the Worker in
  bounded pages; the command sends positions and prints counts, a few hundred
  small requests. It is relative to a cut: a closed 5-minute bucket at or
  after `T0`, by default two buckets before the command starts. Before the cut
  D1 becomes a copy of Turso, including the buckets D1 counted alone since
  `T0`. From the cut on D1 keeps its own rows. `traffic_sources.hits` and
  `counters.actors_total` are set to Turso's value at the cut plus D1's own
  count since the cut, so they equal Turso's at the instant the cut bucket
  began and nothing is counted twice. Each step compares both stores and
  writes only where they differ: a run can stop and resume, a second run
  writes nothing, and without `--apply` it reports and writes nothing. The
  hourly rollup can land during a run: the daily tables are copied last, in
  one transaction with the removal of the D1 buckets Turso has already
  rolled up.
- **Parity** (`POST /admin/stats-parity`, `src/stats-migration/parity.ts`, driven by
  `just stats-parity`). Read-only. It compares closed buckets only: the five
  time-keyed tables row for row, per-day history as the cron reads it, the two
  totals as each store had them at the closed bound, and the `stats_data`,
  `request_data` and `stats_profile` values the cron would write from each
  store at the same instant, including the four figures on the default
  preview image. It exits 1 on any closed difference and prints the rows.
- **Read switch** (`STATS_READ_SOURCE`, `statsReadDb` in `src/stats-store.ts`).
  `"d1"` moves the cron's `materializeStats` (KV values, rollup and prune) and
  `recordProfile`, the `/stats` tail and source list, and the `/source-icon`
  known-source check to D1. Writes still go to both stores, and reading D1
  keeps D1 written even if `STATS_DUAL_WRITE` is switched off.

Both commands read the admin secret from `TYPEAHEAD_SECRET` in `.env`, as
`just smoke` does.

### Operator sequence

1. **Migrations.** This change adds none.
   `bunx wrangler d1 migrations list typeahead-stats --remote` printed "No
   migrations to apply" on 2026-11-12.
2. **Deploy with the switch on Turso.** Merge, push, `just deploy-worker`.
   `wrangler.jsonc` carries `"STATS_READ_SOURCE": "turso"`, so nothing a
   reader sees changes; the deploy adds the two admin endpoints.
3. **Record how the stores agree by themselves.**
   `just stats-parity --since-dual-write` compares only the buckets both
   stores counted since `T0`. It is expected to exit 1 with a few rows; this
   is the drop rate of each store, and the backfill replaces these rows up to
   its cut, so read it before step 5.
4. **Backfill dry run.** `just stats-backfill`. It prints the cut and, per
   page, how many rows differ. Expect about the Turso row counts above.
5. **Backfill.** `just stats-backfill --apply`. If it stops it prints the
   command that resumes it (`--cut N --from STEP:CURSOR`); starting again
   from the beginning is also safe.
6. **Parity.** `just stats-parity` must exit 0. Whatever it prints from the
   cut onward is a write one store dropped; the owner decides whether that is
   acceptable (question 3 of the plan). To remove those differences, run step
   5 again, which takes a newer cut, and run parity straight after. Run parity
   once more after a top-of-hour cron, so one comparison includes a rollup on
   Turso.
7. **Flip.** Run step 5 and step 6 again immediately before, so the cut is
   minutes old. Set `"STATS_READ_SOURCE": "d1"` in `wrangler.jsonc`,
   regenerate `src/public-worker-env.d.ts` with the command in its header,
   commit, push, `just deploy-worker`.
8. **Verify `/stats`.** Before the flip save
   `curl -s 'https://typeahead.waow.tech/stats?range=all'`; after it (the page
   is edge-cached for 60 seconds) the actors indexed, searches, and
   "requests · apps" figures must be the same apart from traffic since.
   `/stats?range=1h` must show the current bucket, which now comes from D1.
   The KV history is still the copy Turso produced until the next
   top-of-hour cron; after that cron, check its `cron_done` line carries
   `materializeStats` and `recordProfile` with no `cron_step_failed`, load the
   page and the preview image (`/og-image`) again, and run `just stats-parity`.
9. **Watch.** `stats_write_failed` with `store: "d1"` (a dropped D1 write is
   now a count missing from the page), `stats_read_source_unavailable` (the
   binding is missing and reads fell back to Turso), `cron_step_failed` for
   `materializeStats` or `recordProfile`, and the newest D1 snapshot: the
   hourly snapshot's D1 write is swallowed on failure, so check
   `SELECT max(hour) FROM snapshots` against the hour. Run `just stats-parity`
   daily while dual write lasts.

**To flip back:** set `"STATS_READ_SOURCE": "turso"`, regenerate the types,
commit, push, `just deploy-worker`. Turso has been written throughout, and
its rollup resumes at the next cron. The page moves by whatever the two
stores counted differently since the last backfill cut. Before flipping
forward again, repeat steps 5 and 6.

### What still reads Turso after the flip

These read the actor corpus, which stays in Turso until issue 31, and they are
unchanged by the switch:

- `countSnapshot` (`src/enrichment.ts`): one Turso batch reading
  `counters.actors_total` and three counts on `actors` (`hidden != 0`, empty
  handle, empty avatar). Its result is written to `snapshots` in both stores.
  It needs the running actor total and those three counts from whoever owns
  the corpus. D1 holds a dual-written `actors_total`, but the snapshot does
  not read it.
- `recordExclusions` (`src/exclusions.ts`): `actors` rows with `hidden != 0`,
  `mod_overrides`, blocked domains and the two identity counts, written to KV
  `search_exclusions`. It needs those counts published.
- The actor deltas are computed from corpus writes: `changes` of the
  `INSERT OR IGNORE INTO actors` probes in `/admin/ingest`, and deletes in
  `/admin/delete` and the cron's dead-actor cleanup.
- `counters.label_stream_seq`, the ingester's label cursor
  (`src/account-labels.ts`), shares the `counters` table with `actors_total`.
  It is not a statistic and is never copied; it has to move or stay before
  the table is dropped in step 8 of the plan.
- Every statistics write still goes to Turso as well as D1 until step 7 of
  the plan.

## Slot control

Moderation overrides, blocked domains and indexing requests are recorded in
the `typeahead-control` D1 database, bound as `CONTROL_DB`, and pulled by every
store slot from `/slot/control/*` behind `SLOT_CONTROL_SECRET`. The record, the
endpoints, what a slot does with them, what to alert on, the rollout and its
undo are in [slot control](slot-control.md). As of 2026-11-12 this is built and
tested on the `control-pull` branch and none of it is deployed: the database
does not exist, and the admin endpoints and `/request-indexing` still write
only Turso, which store slots do not read.

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

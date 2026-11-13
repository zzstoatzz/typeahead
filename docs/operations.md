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

`first_check` is the pass that queues a refresh for every actor no profile
source has answered for, once in the life of a store: its hourly ceiling
(`per_hour`), the check time it looks for (`stamp`) and the one its stored
progress was made for (`stored_stamp`), when it started and finished, actor
records `examined` and actors `queued`, and the last DID it looked at. It is
done when `finished_at` is set. A store loaded by a bootstrap built before
2026-11-12 holds those actors stamped as checked at the load: set
`ENRICH_FIRST_CHECK_STAMP` to that time (`profile_checked_at` of any actor
the slot has not refreshed since, as `/store/actor` reports it) and the pass
starts over for them. `ENRICH_FIRST_CHECK_PER_HOUR=0` stops it; its position
is kept. Not deployed as of 2026-11-12.

`profile_fill` is the pass that copies the descriptions and banners Turso
holds into the actors that have none, once in the life of a store
([Turso retirement](turso-retirement.md#profile-fill)): its `state` (`on`,
`off`, or `misconfigured` with `problem`), whether it has `finished`, when it
started and finished, `pages` applied, the `rows` they carried, actors
`filled`, the last DID the Worker read, and while it runs the page size, the
step interval, the bridge lag limit, steps `deferred` for it and `failures` by
kind. Off unless `PROFILE_FILL=1`. Not deployed as of 2026-11-12.

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
- One variable, `STATS_MODE`, says which stores are written and which is read
  (`statsMode` in `src/stats-store.ts`):

  | `STATS_MODE` | Written | Read |
  | --- | --- | --- |
  | `turso-only` | Turso | Turso |
  | `read-turso` (in `wrangler.jsonc` now) | Turso and D1 | Turso |
  | `read-d1` | Turso and D1 | D1 |

  A missing or unknown value is `turso-only`. `read-d1` without the `STATS_DB`
  binding fails `/stats`, `/source-icon` and the two statistics cron steps
  instead of answering from Turso.
- The statistics written are `request_buckets`, `metrics`, `traffic_sources`,
  `traffic_buckets`, `actor_deltas`, `counters.actors_total` and the hourly
  `snapshots` row. On the request path each store's write is its own
  `waitUntil` with its own swallowed failure, logged as `stats_write_failed`
  with `store` set to `turso` or `d1`.
- "Read" covers `/stats`, `/source-icon`, the KV materialization and the
  90-day rollup and prune, which run only on the store that is read.
- To stop the D1 writes without a code change, deploy with
  `--var STATS_MODE:turso-only`. A later plain deploy restores the value in
  `wrangler.jsonc`.
- To check that rows are landing (read-only):
  `bunx wrangler d1 execute typeahead-stats --remote --command "SELECT datetime(bucket * 300, 'unixepoch') AS bucket_utc, sum(hits) AS answers FROM request_buckets GROUP BY bucket ORDER BY bucket DESC LIMIT 6"`.

## Statistics read cutover to D1

### Rollout commands

Run in order from a checkout with this branch merged and `TYPEAHEAD_SECRET`
in `.env`. A line whose condition is not met stops the rollout. The steps
are explained under [Sequence](#sequence).

| | Command | Pass condition |
| --- | --- | --- |
| 1 | `just control-ready` | exits 0. `just deploy-worker` runs it first, and this deploy carries the slot control Worker code if that is not live yet: finish steps 1 to 4 of the [slot control rollout](slot-control.md#rollout) before step 3 |
| 2 | `bunx wrangler d1 migrations list typeahead-stats --remote` | prints "No migrations to apply" |
| 3 | `just deploy-worker` | deploys `STATS_MODE=read-turso` (the value in `wrangler.jsonc`); both smoke suites pass |
| 4 | `just stats-parity --since-dual-write` | not a gate: exit 1 with a few rows is expected; keep the output |
| 5 | `just stats-backfill` | ends with "dry run complete: nothing written" |
| 6 | `just stats-backfill --apply` | ends with "backfill complete"; if it prints "resume with:", run that command |
| 7 | `just stats-parity` | **the gate**: exits 0 and ends with "parity: the stores agree on all closed data". On exit 1, run step 6 again, then this |
| 8 | `just stats-parity`, after the next top-of-hour cron | exits 0 |
| 9 | `just stats-backfill --apply && just stats-parity`, minutes before the flip | exits 0 |
| 10 | `curl -s 'https://typeahead.waow.tech/stats?range=all' > stats-before.html` | saved for step 12 |
| 11 | `sed -i '' 's/"STATS_MODE": "read-turso"/"STATS_MODE": "read-d1"/' wrangler.jsonc && bunx wrangler types src/public-worker-env.d.ts --env-interface PublicWorkerEnv --include-runtime=false --env-file /dev/null && git commit -am 'stats: read statistics from D1' && git push && just deploy-worker` | the flip: both smoke suites pass |
| 12 | `curl -s 'https://typeahead.waow.tech/stats?range=all' > stats-after.html`, a minute after the deploy | actors indexed, searches and "requests · apps" match step 10 apart from traffic since |
| 13 | `just stats-parity`, after the next top-of-hour cron | exits 0, and that cron's `cron_done` has no `cron_step_failed` |

Rollback, at any point after step 11:

| | Command | Effect |
| --- | --- | --- |
| now | `npx wrangler deploy --var STATS_MODE:read-turso` | reads return to Turso without a commit; it lasts until the next plain deploy |
| then | `git revert --no-edit <the step 11 commit> && git push && just deploy-worker` | makes `read-turso` the committed value again |
| D1 writes off as well | `npx wrangler deploy --var STATS_MODE:turso-only` | Turso alone is written and read |

Before flipping forward again, repeat steps 9 to 13.

Steps 3 to 6 of the [plan](plans/stats-off-turso.md), which holds the design,
the measured sizes and costs, and the query plans. Status on 2026-11-12: the
code is on the `stats-d1-read` branch, tested locally, not deployed; no
backfill has run and production reads Turso.

Two commands do the work. Both call an admin endpoint of the Worker, so the
rows move inside Cloudflare, and both read the admin secret from
`TYPEAHEAD_SECRET` in `.env`, as `just smoke` does.

- `just stats-backfill` copies the history from Turso into D1. It is a dry
  run unless `--apply` is given, it can be stopped and run again, and a
  second run writes nothing.
- `just stats-parity` compares the two stores and writes nothing. It exits 1
  when closed data differs and prints the rows.

### Sequence

1. **Migrations.** None to apply:
   `bunx wrangler d1 migrations list typeahead-stats --remote`.
2. **Deploy.** Merge, push, `just deploy-worker`. `wrangler.jsonc` carries
   `"STATS_MODE": "read-turso"`, which is what production does today, so
   readers see no change; the deploy adds the two endpoints.
3. **See how the stores agree by themselves.**
   `just stats-parity --since-dual-write`. Expect exit 1 with a few rows:
   each store drops about one write in ten thousand. The backfill replaces
   these rows, so look now.
4. **Dry run.** `just stats-backfill`.
5. **Backfill.** `just stats-backfill --apply`. If it stops, run the resume
   command it prints.
6. **Parity.** `just stats-parity` must exit 0. A difference it prints is a
   write one store dropped after the backfill's cut; running step 5 again
   takes a newer cut and removes it. Run parity once more after a
   top-of-hour cron.
7. **Flip.** Repeat steps 5 and 6 immediately before. Set
   `"STATS_MODE": "read-d1"` in `wrangler.jsonc`, regenerate
   `src/public-worker-env.d.ts` with the command in its header, commit, push,
   `just deploy-worker`.
8. **Verify.** Save `curl -s 'https://typeahead.waow.tech/stats?range=all'`
   before the flip. After it (the page is edge-cached for 60 seconds) actors
   indexed, searches and "requests · apps" must match apart from traffic
   since, and `/stats?range=1h` must show the current bucket. After the next
   top-of-hour cron, check `cron_done` lists `materializeStats` and
   `recordProfile` with no `cron_step_failed`, load `/stats` and `/og-image`
   again, and run `just stats-parity`.
9. **Watch**, while both stores are written:
   - `stats_write_failed` with `store: "d1"`: a count missing from the page.
   - `cron_step_failed` for `materializeStats` or `recordProfile`.
   - The hourly snapshot's D1 write is swallowed on failure, so compare
     `SELECT max(hour) FROM snapshots` in D1 with the current hour.
   - `just stats-parity` daily.

**To flip back:** set `"STATS_MODE": "read-turso"`, regenerate the types,
commit, push, `just deploy-worker`. Turso was written throughout and its
rollup resumes at the next cron; the page moves by what the two stores
counted differently since the last backfill. Repeat steps 5 and 6 before
flipping forward again.

### What still reads Turso after the flip

These read the actor corpus, which stays in Turso until issue 31. The mode
does not move them.

| Reader | Reads | Needs from the corpus owner |
| --- | --- | --- |
| `countSnapshot` (`src/enrichment.ts`), hourly | `counters.actors_total` and three counts on `actors` (`hidden != 0`, empty handle, empty avatar), one batch | the running actor total and the three counts |
| `recordExclusions` (`src/exclusions.ts`), daily | `actors` rows with `hidden != 0`, `mod_overrides`, blocked domains, two identity counts | those counts |

Also still on Turso: every statistics write (until step 7 of the plan); the
actor deltas, which are computed from corpus writes in `/admin/ingest`,
`/admin/delete` and the cron's dead-actor cleanup; and
`counters.label_stream_seq`, the ingester's label cursor, which shares the
`counters` table and has to be dealt with before that table is dropped.

### Removing the tooling

The backfill and parity code is temporary. When Turso's statistics tables are
gone, one commit deletes `src/stats-migration/`, `scripts/stats-d1.ts`, the
`statsMigrationRoute` call in `src/index.ts`, the two `just` recipes and this
section.

## Slot control

Moderation overrides, blocked domains and indexing requests are recorded in
the `typeahead-control` D1 database, bound as `CONTROL_DB`, and pulled by every
store slot from `/slot/control/*` behind `SLOT_CONTROL_SECRET`. The record, the
endpoints, what a slot does with them, what to alert on, the rollout and its
undo are in [slot control](slot-control.md). As of 2026-11-12 this is built and
tested on the `control-pull` branch and none of it is deployed: the database
does not exist, and the admin endpoints and `/request-indexing` still write
only Turso, which store slots do not read.

## Turso retirement

What the Worker still reads and writes in Turso, the slot endpoints that
replace the reads (`/actors`, `/corpus`), the three variables that move each
feature over (`PROFILE_MODE`, `CORPUS_MODE`, `TURSO_ACTORS`), the parity checks
and the order in which Turso, the old ingester and the HeavyPad flows are
stopped are in [Turso retirement](turso-retirement.md). As of 2026-11-12 this
is built and tested on the `turso-reads` branch and none of it is deployed:
every feature listed there still runs against Turso.

## Search exclusion counts

`/stats` shows two figures from the daily `recordExclusions` cron step
(`src/corpus.ts`, KV `search_exclusions`), logged as `exclusions_recorded`.
What follows describes the count taken in Turso (`src/exclusions.ts`), which
is what runs unless `CORPUS_MODE` is `slot`; the slot's count is described in
[Turso retirement](turso-retirement.md#what-an-app-sees-in-slot-mode):

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

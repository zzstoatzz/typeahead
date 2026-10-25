# typeahead

Community actor search for [atproto](https://atproto.com).

[Search](https://typeahead.waow.tech/) · [Stats](https://typeahead.waow.tech/stats) · [API docs](https://typeahead.waow.tech/docs) · [Machine-readable docs](https://typeahead.waow.tech/llms.txt)

```sh
curl 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=10'
```

Returns the `profileViewBasic` fields without viewer state: `did`, `handle`,
`displayName`, `avatar`, `associated`, `labels`, and `createdAt`. The
[self-owned lexicon](lexicons/tech.waow.typeahead.searchActors.json) defines the
canonical endpoint. `/xrpc/app.bsky.actor.searchActorsTypeahead` remains a
compatible alias with identical parameters and response.

**Live since October 20, 2026, 15:19 UTC:** optional `pds=acme.com`
searches by indexed hosting PDS before candidate selection, including custom-domain
handles. The snapshot builds scopes only for PDSs with at most 10,000 known actor
rows at snapshot time (`INDEX_PDS_MAX_ACTORS`, configurable). Larger PDSs return
503 `PdsScopeUnavailable`; their global results remain available. The PDS-scoped lists are additional to the global lists; requests without
`pds` retain their existing behavior. Unknown hosts return an empty list; missing
backend/snapshot support returns 503 instead of unfiltered results. Hostnames are
case-insensitive and exact (no subdomain expansion); HTTP(S) origins and a trailing
slash/dot are accepted, ports and paths are rejected. See the
[correctness/performance evaluation and rollout gates](docs/evals/pds-scope.md).
The homepage filter sits inside the search field and stays closed by default,
including when a shared URL selects a PDS. Opening it reveals a hostname panel
without changing the page layout. An available PDS favicon
replaces the filter icon while selected; missing icons retain the sliders. See the
[compact integration review](docs/evals/pds-homepage-20261020.md).
Full-corpus qualification covered 12.64 million searchable actors, 2,223 eligible
PDS hosts, paired global/scoped comparisons, rollback and empty-root restoration.
The public API, browser flow and Logfire spans were verified after deployment.

PDS membership is incomplete: the October 20 source export had 9.88 million
of 12.65 million actor rows without a hosting PDS. The weekly identity job
selected missing handles rather than missing PDS values. Two guarded repairs
have been verified; full repair and recurring coverage remain unfinished. Repair execution is stopped
after [cleanup of abandoned experiments](docs/pds-repair-cleanup-20261020.md).
The retained [repair tooling](docs/pds-identity-repair.md) includes transfer limits,
conditional writes and durable receipts. These tools do not enable bulk writes
or change the deployed 10,000-actor filter cutoff.
The [October 20 parser-fix cutover](docs/evals/pds-parser-cutover-20261020.md)
rejects failed replay queries before declaring readiness. Its previous PDS-capable
backend remains running for rollback; smoke, scoped aliases and Logfire were
verified after the transition.

Profile cards use `/xrpc/app.bsky.actor.getProfiles?actors=…` (up to 25 actors)
or `/xrpc/app.bsky.actor.getProfile?actor=…`. They include available profile
and enrichment fields; counts are omitted until enriched. No viewer state.

## How it works

```mermaid
flowchart LR
  Clients -->|search| Worker[Cloudflare Worker]
  Relay[Bluesky relay] -->|subscribeRepos| Ingester[Fly ingester]
  Ingester -->|batches and cursor| Worker
  Worker -->|actor writes and accounting| Turso
  Worker -->|cache misses, private binding| Transport[Origin transport Worker, US East]
  Transport --> Search[Fly search]
  Turso -->|changed rows and tombstones| Search
  Turso -->|database export| Builder[Heavypad index builder]
  Builder -->|index.db and manifest| R2
  R2 -->|verified snapshot| Search
```

Search response timing and the October 20 cold-edge investigation are recorded in
[the latency evaluation](docs/evals/edge-latency-20261020.md). Engine timings alone
do not establish public request latency. The homepage opens results after the
answer arrives, without a transient loading dropdown.

- **Worker:** public endpoints, a 60-second search cache, admin ingestion,
  request accounting, and profile APIs. Turso search fallbacks are opt-in.
  Cache misses use a private transport Worker placed in US East to avoid
  measured overseas origin detours; cache hits stay at the edge.
  Bluesky is an explicit rollback option, not the normal search backend.
- **Ingester:** Zig on Fly, consuming the relay's `subscribeRepos` firehose
  (not Jetstream). Profile and activity events become batched actor updates.
  Failed writes retain their queues and apply backpressure; startup resumes
  from the persisted relay cursor. Identity events enter the protected discovery
  queue, and each one is re-resolved from its DID authority with the handle
  verified both ways, so a handle change lands within seconds. Watchdog exits and
  panics terminate on a deadman that does not depend on telemetry flushing.
- **Turso:** durable actor records, moderation overrides, and tombstones.
  Ingestion, enrichment, and request accounting share its writer.
- **Index builder:** the same Zig codebase in `MODE=indexer`, orchestrated by
  Prefect on heavypad with a three-day schedule, temporarily paused October 20
  while bulk work moves to cloud infrastructure. It exports Turso, builds prefix-to-actor
  lists and compact profiles, and publishes a SQLite snapshot to R2. It is an
  offline batch job; an unsuccessful build leaves the previous snapshot serving.
- **Search:** Zig on Fly. It verifies and attaches the R2 snapshot read-only,
  then pulls newer Turso rows into a mutable overlay approximately every five
  minutes. Queries merge snapshot and overlay candidates, promote exact handles,
  hydrate profiles, and apply moderation. There is no separate full actor-table
  replica on the serving machine; the snapshot itself contains compact profiles.
- **Maintenance:** Prefect jobs on heavypad handle identity and profile
  enrichment. Weekly PLC processing is temporarily paused with the bulk builder;
  its existing selector covers rows with no handle yet
  (it does not correct stale handles; the ingester does). The hourly Worker cron handles
  freshness, statistics, and moderation refresh—not bulk enrichment.

Compact snapshots have served production since October 17, 2026, 19:37 UTC. Candidate `cc2d5a1` has passed independent disaster restoration, legacy
binary rollback, complete live replay, restart admission, the isolated ranking
suite, and 224 current-Worker alias/cache checks. Its complete 180-trial cold
campaign passed all primary gates; compact startup took about 205 seconds, so
cutover requires an already-ready replacement.

The [uncapped warm run](docs/evals/compact-cancel-fixed-warm/gate.json) remains
inconclusive under the unchanged no-regression gate: mixed-read p95 upper
confidence bound is +3.5%. Native matched reload qualification passed all 60 trials with no secondary
regression flags.
The unmodified legacy performance control crashed during concurrent reads;
that comparison is incomplete, with its failure retained. Nate accepted these
two exceptions on October 17, conditional on the now-passing reload gate.
The production cutover passed freshness, alias, and full smoke checks. The October 19 scheduled compact publication completed; native admission, live catch-up and empty-directory recovery passed. Temporary
qualification resources were removed. Source integration and retained recovery
resources are recorded in the cutover runbook. The October 18 full-day
counter review covered all 288 five-minute buckets: 14,902 successful searches,
205 rate-limited searches, and no recorded 5xx responses across endpoints.
Compact Logfire telemetry was restored October 19 and actual sampled search spans
verified against its deployed image. The earlier counters do not establish
historical full-day latency or backend error coverage. The
October 19 publisher registration used `my-prefect-server` main `e4275b5`, whose
full CI workflow passed through registration; its October 19
09:00 UTC scheduled run completed at 11:25 UTC with the compact format and
qualified source tag.
See the [cutover runbook](docs/plans/compact-production-cutover.md) for current
inventory and evidence, and the [rollout gates](docs/plans/snapshot-compaction-rollout.md)
for acceptance criteria. Earlier candidates' passing results do not qualify this
replacement.

As of October 20, 16:31 UTC, Worker `89901546-0e01-4789-b19a-541f0545460e`
serves PDS-capable build `b1792484789-3d65-px05` through the private
`typeahead-search-transport` binding and `typeahead-search-compact.fly.dev`, machine `8d4916be311008`, following root
`latest.json`. Its snapshot and live overlay are under `/data/pds-current-20261020`
and `/data/pds-overlay`. Public scoped/alias checks, authenticated smoke, native
admission and catch-up, browser checks, and compact Logfire spans passed.
The publisher is registered from `my-prefect-server` main `776c8e1`, with immutable
builder tag `pds-compact-31ec142-20261020`; its next queued run is October 22 at
09:00 UTC was verified at that time. On October 20 at 23:27 UTC, both bulk
home schedules were verified inactive and all queued runs removed to avoid ISP
overage traffic. MPS main `7fedc3b` records the hold. The duplicate cloud
reconstruction and failed checkpoint chain were stopped during cleanup; a
replacement recurring publisher
and identity repair are not yet enabled. The existing snapshot and live overlay
continue serving. Do not resume the old schedules before qualifying the cloud
replacement. See the [operational record](docs/evals/pds-parser-cutover-20261020.md).

Legacy remains running and synchronized. Old compact machine `781e732c264678`
is retained privately on a protected pre-PDS pointer with its original overlay.
Before routing to an incompatible backend, disable PDS search on the current
Worker so even cached scopes fail explicitly; never restore a pre-PDS Worker
that silently ignores the parameter. Preserve newer unrelated Worker changes.
See the [PDS release runbook](docs/plans/pds-homepage-rollout.md) for rollback,
retained recovery artifacts and evidence. `services/fly.search-compact.toml`
records the current configuration; `just deploy-search` remains guarded.

The current compact pair is 7.204 GB. Local legacy twins remain mandatory for
recovery; two complete current-format pairs need about 46.6 GB before overlay
and temporary files, so smaller production volumes are not yet justified.
Empty-overlay recovery also requires a snapshot inside the seven-day tombstone
replay window; archived images alone do not preserve that window. The previous
recovery resources remain under the existing October 24/26 retention gates.

The offline converter uses a Zig shared library for packing and exhaustive
record verification; Python validates manifests and stages the finished pair.
Build it with `just build-compactor`, test with `just test-compactor`, and use
`just convert-quiet --source ... --manifest ... --output ... --build-id ...` on
HeavyPad to cap the entire conversion at 20% of one CPU. See
[native conversion](docs/native-compaction.md) for the format contract,
measurements, and scheduler integration status.

The experimental compact reader caches at most 128 ordinary 8 KiB pages per
read-pool slot (1 MiB each, 8 MiB with eight slots). Each exclusive lease owns its
slot cache; snapshot activation invalidates every slot before new leases can
read. Oversized pages use the existing bounded scratch path. This remains
unshipped pending the complete release gates; see
[the diagnostic evidence](docs/evals/compact-read-cache.json) and
[exact-release recovery checks](docs/evals/compact-cache-release-checks.json).

The snapshot precomputes search candidates. Union-prefix overlay additions use a
partial `(key, score DESC, did ASC)` index and stop after the top 50, plus indexed
corrections to snapshot candidates. Exact keys and intersections retain full
matching-key reads to preserve ranking correctness. Hydration is capped at 50
before exact-handle reinjection. Queries longer than 16 bytes use their 16-byte
prefix key and filter hydrated rows against the full query. See
[architecture](docs/architecture.md) and [lookup evaluation](docs/evals/bounded-overlay.md).
The ranking index was installed on the production overlay on October 9.
Its idempotent initialization change is on `codex/overlay-ranking`; the running
service image was not replaced.

## Public pages and metadata

The Worker serves the homepage (`/`), `/stats`, `/docs`, `/reputation`, and
`/llms.txt`, all from `src/pages/` on one shared theme. Dated state:

- Ranking explainer (October 20, 2026): `/reputation` has a three-row summary
  with small section icons and expandable weights. Entering a handle collapses the guide; account reports
  show score, visibility, and moderation, followed by expandable details.
  [Deployment verification](docs/evals/ranking-page-20261020.md). Ranking and
  the report API are unchanged.
- Homepage navigation (September 6, 2026): `[src]` and `[llms.txt]` share the
  project-resource row; the theme control sits at the right; attribution,
  stats, and docs are in the footer.
- Link previews (September 20, 2026): every page's description leads with what
  typeahead does and where it looks, and does not define the service by what
  it replaces. The card at `/og-image` (`src/handlers/og.ts`) reads the hourly
  stats KV blob, never Turso, and shows four cumulative figures: actors indexed
  with handle coverage, avatar coverage, apps that have ever sent a search, and
  searches answered all time. The app count is a filtered `traffic_sources`
  count the cron materializes as `traffic_sources_total`. Embedders cache cards
  by URL, so `CARD_VERSION` in `src/pages/theme.ts` must be bumped whenever the
  card changes.
- App metadata (September 20, 2026): the mark is served at `/favicon.svg`,
  rasterized at `/apple-touch-icon.png`, and declared in `/site.webmanifest`.
  A `data:` favicon preceded this; crawlers and home-screen installs could not
  read it.

"Hidden by moderation" left the card on September 20, 2026. That count is
label observations from the refresh cron, not enforcement: PDS-level
suspensions arrive as `#account` events and are deleted, and appview takedowns
are re-enriched visible by policy. See
[operations](docs/operations.md#the-hidden-count-is-sampling-not-enforcement-2026-09-20).

Stats interactions (October 17, 2026) keep the donut stationary during repeated
selections. The activity card uses aligned search/latency metrics and compact
comparison pills whose DOM nodes and icon slots remain stable during selection.
The larger SVG ring preserves twelve sources plus a remainder, with exact source
details shown on selection rather than printed on slices. “Explore … more”
zooms into the remainder; “Back to top apps” returns directly to the original
view at any depth, and breadcrumbs return to intermediate levels. This action does
not depend on identifying a color. Selected apps have checkmarks and selected
slices have outlines. Brand icons and operator avatars appear on slices where
they fit; the same sources are used in the pills. Icons remain upright, and
resize/drill-down recomputes fit without crowding narrow slices.

One complete app pill list replaces the duplicate table. Search, batches of 50,
and show-all expose every app. A “Show all / Show fewer” control above the
list stays synchronized with the bottom controls, so collapsing never requires
scrolling to the end. Pills retain percentages; selection shows exact
request counts and shares together inside the ring, with its website link. The
header keeps overall totals visible. Deselecting the inspected app returns the
center to the total rather than an older comparison; the repeated root breadcrumbs and detached
detail row are removed. Long names wrap up to three lines in the center; the
complete name remains in its pill. Six verified brand API mappings supply icons,
with domain favicon and marker fallbacks. See
[the source chart and brand findings](docs/ui/stats-sources.md).

Source totals count cache misses; endpoint/status totals count all answered
requests. Comparisons, total-line visibility, and link/image/CSV exports remain
available. Secondary text has stronger contrast, the mobile moderation metric
spans its row, and the footer has one short “Add your app” link.

The October 19 follow-up matches the homepage’s 16px search input text to avoid
iPhone focus zoom while preserving pinch zoom. Deployed as Worker version
`86590349-5a6e-42c1-a6de-4c5d8835ff3b`. Deployment smoke passed. Live mobile and desktop checks verified logo loading,
correct slice placement, and tapping directly on an icon. The release preserves the deployed freshness fix and current backend routing. Live Chromium and WebKit checks cover selection, stable geometry,
recursive navigation, pagination through all apps, search, shared selections,
and dimensions in both themes at phone and desktop widths. Full-page visual
checks include 320px, 390px, and 1280px layouts.

Browser regression: `node scripts/test-stats-interactions.mjs <stats-page-url>`
(requires Playwright with Chromium and WebKit; `PLAYWRIGHT_MODULE` may point to an
existing installation).

## Operational status

October 25, 2026: the improved-startup backend is being prepared on a separate
Fly machine and volume with its own frozen snapshot pointer. Production remains
on the original backend and stale snapshot; restoring availability did not resolve
the freshness alert. The [held cutover plan](docs/ops/search-cutover-20261025.md)
records isolation, qualification gates, rollback, and cleanup. No production
routing, transport deployment, or shared snapshot publication is authorized during
preparation. Earlier startup results below refer to the previous snapshot;
qualification of the fresh snapshot is recorded separately before approval.

As of September 6, 2026:

- Search runs on one Fly machine: 4 shared CPUs, 2 GiB RAM, and a 60 GB volume.
  The isolated [startup experiment](docs/evals/startup-buffer-20261024.md) tests
  buffered validation and metadata-only recovery; neither is deployed to production.
  Its [parallel candidate](docs/evals/startup-admitted-candidate-20261024.md) adds
  installation-time admission and block checksums before production qualification.
  Ingester runs separately on 1 shared CPU and 256 MiB RAM.
- The ingest retry/cursor fixes are deployed. Historical recovery restored and
  search-verified 377 accounts; remaining historical uncertainty is documented
  in the [recovery closeout](docs/retros/2026-09-05-ingester-recovery-closeout.md).
- Bulk profile enrichment is paused following writer contention. A guarded
  workflow is prepared locally in `my-prefect-server`; it is not deployed.
- The bounded overlay lookup went live on the serving machine on
  2026-09-09 05:34 UTC: it was on main and shipped with the deadman exit-path
  deploy, ahead of the staged rollout the canary was preparing. `/debug/search`
  reports `overlay_bounded: true` on union keys. Before that it had passed the
  shadow canary's 18/18 live-query comparisons, all 36 full-overlay query cases
  (11.16M prefix rows) and 108 concurrent-update comparisons. The ranking
  index is NOT installed in production: it adds 543 MiB and increases
  projection write work, and without it SQLite still visits and sorts every
  matching prefix row before the bound applies. The stopped canary machine and
  its temporary 60 GB volume were deleted on 2026-09-28; evaluation evidence is preserved and the ranking-index experiment
  remains deferred. See the [evaluation](docs/evals/bounded-overlay.md)
  and [canary limits and rollback](ops/canary/README.md).

An isolated [snapshot compaction experiment](docs/evals/flat-prefix-compaction.md)
preserves the serving snapshot's contents with 51.4% less disk than SQLite VACUUM.
All declared read-speed gates pass; the experiment is not deployed. The
[rollout plan](docs/plans/snapshot-compaction-rollout.md) covers paired artifacts,
production qualification, and overlay-safe rollback.

These are dated observations, not configuration sources. Fly role configs live in
`services/`; Prefect schedules and deployment definitions live in
[`my-prefect-server`](https://tangled.org/zzstoatzz.io/my-prefect-server).

## Development

```sh
bun install
just check             # Worker types, lint, tests, deployment dry-run
just test-services     # Zig unit tests
bunx wrangler dev      # local Worker
```

The Zig binary requires `MODE=ingester`, `MODE=search`, or `MODE=indexer`.
There is no default role. Use the role's configuration and credentials; a bare
`zig build run` is not a configured local service.

- `src/` — Worker handlers, public pages, and shared page theme.
- `services/` — Zig ingester, search, and index builder.
- `deploy/home-indexer/` — heavypad prerequisites.
- `scripts/` — smoke checks, benchmarks, recovery tools, and offline experiments.
- `ops/` — health exporter and dashboard tooling.
- `docs/` — architecture, operations, evaluations, and dated retrospectives.

Repository instructions live in [AGENTS.md](AGENTS.md).
`.claude/CLAUDE.md` is a symlink to that same file.

## Deployment and validation

```sh
just deploy-worker     # Cloudflare deploy, then smoke checks
just deploy-ingest     # Fly ingester
just deploy-search     # Fly search
```

These commands affect production. Search currently has a single serving machine;
use an isolated target for performance experiments and account for availability
before replacing it. Benchmarks and smoke scripts may default to production;
check their target and whether they perform writes before running them.

The home builder is deployed through Prefect; its bulk schedule is currently
paused for the cloud migration described above. `services/fly.indexer.toml` remains
an optional manual Fly rollback path, not the scheduled builder. See
[operations](docs/operations.md).

## Costs

As verified October 24, 2026, Fly runs one search machine (four shared CPUs,
2 GiB RAM) and one ingester (one shared CPU, 256 MiB RAM).
Three stopped rollback/legacy machines and their 60 GB volumes were deleted;
recovery uses retained R2 artifacts and saved configurations. See the
[retirement record](docs/operations.md#search-rollback-resource-retirement--october-24-2026).

The existing [hub cost snapshot](https://hub.waow.tech/api/costs.json) reports
project-attributed line items and their estimation basis. Turso and Cloudflare
subscriptions are shared with other projects; their whole account invoices are
not Typeahead's cost. [COSTS.md](COSTS.md) retains dated historical research;
do not use its older resource sizes or totals as current estimates.

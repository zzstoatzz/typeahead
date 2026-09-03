# operations / edge gotchas

Operational notes that aren't obvious from the code, because the relevant
config lives in Cloudflare/Fly dashboards, not the repo.

## Cloudflare: Browser Integrity Check is OFF for the public API (`/xrpc/`)

**The drop-in search API must accept any HTTP client** (scripts, server-side
consumers, not just browsers) — that's the whole point of being a Bluesky
typeahead replacement, and it matches bsky's public-API posture.

Cloudflare's **Browser Integrity Check (BIC)** was on for the `waow.tech`
zone and returned **`HTTP 403`, body `error code: 1010`** ("banned based on
browser signature") to non-browser fingerprints — e.g. Python's default
`urllib` client. `curl` and the Zig ingester happened to pass, so it looked
fine until tested with a plain script.

**Fix (live):** a Cloudflare **Configuration Rule** named **`xrpc allow`**
(zone `waow.tech`, id `8c32e078cab2a47dcaeaf5284ca0aa54`, phase
`http_config_settings`):

- **Match:** `(http.host eq "typeahead.waow.tech" and starts_with(http.request.uri.path, "/xrpc/"))`
- **Action:** set **Browser Integrity Check → Off**

Scoped on purpose: BIC stays **on** for the HTML pages (`/`, `/stats`,
`/docs`) and for `/admin/*` (which is also Bearer-token auth'd). Only the
public, read-only `/xrpc/*` API is opened.

**If this rule is deleted or its toggle flips back to On**, every non-browser
API client breaks with `403 / error code: 1010`. Verify with a bare client:

```sh
python3 -c "import urllib.request,json; print(json.load(urllib.request.urlopen('https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=nate&limit=2'))['actors'][0]['handle'])"
```

Expect a handle (200), not an `HTTPError 403`.

**Note:** the `cloudflare-api` MCP / OAuth token cannot read or write this
zone's settings or rulesets (returns `9109` / `10000` / "request is not
authorized") even with full-access scopes selected — this rule must be
managed in the dashboard (Rules → Configuration Rules).

**For volume/abuse control** use a **Rate Limiting Rule** on `/xrpc/*`
(requests/min per IP) — BIC only fingerprints, it does not bound volume.

## Cloudflare: Browser Cache TTL must "Respect Existing Headers"

The worker sets `Cache-Control: public, max-age=60` on search responses
(`src/handlers/search.ts`) so the edge + browser cache results for ~1 min and
moderation/profile/deletion changes go live within that window.

The `waow.tech` zone's **Browser Cache TTL** setting was `14400` (4 hours, a
legacy CF default), which **overrides** the worker's `max-age` — the served
header became `max-age=14400` and stale results persisted on the edge per-POP
for up to 4h. Symptom: a just-applied moderation change (e.g. a domain block)
keeps showing in the UI long after Turso + the search service are correct;
`curl` of a cache-busted URL returns the fresh value but the cached query URL
does not.

**Fix (live):** Browser Cache TTL set to **0 = "Respect Existing Headers"**
(Caching → Configuration), so the worker's `max-age=60` is honored. After
changing it, **purge the cache** to clear the inflated entries.

Unlike the BIC rule above, this **is** doable via API with a properly-scoped
token (the MCP's own token still can't). Mint a zone-scoped token for
`waow.tech` with permission groups **Cache Purge**, **Cache Settings : Edit**,
**Zone Settings : Edit**, put it in gitignored `.env` as `CF_CACHE_TOKEN` —
**not** `CF_API_TOKEN`/`CLOUDFLARE_API_TOKEN`, which `bunx` auto-loads from
`.env` and wrangler uses for `deploy` (a cache-scoped token under that name
breaks `bunx wrangler deploy`). Then:

```sh
set -a; . ./.env; set +a
ZONE=8c32e078cab2a47dcaeaf5284ca0aa54
# inspect / fix the setting
curl -sS "https://api.cloudflare.com/client/v4/zones/$ZONE/settings/browser_cache_ttl" -H "Authorization: Bearer $CF_CACHE_TOKEN"
curl -sS -X PATCH "https://api.cloudflare.com/client/v4/zones/$ZONE/settings/browser_cache_ttl" -H "Authorization: Bearer $CF_CACHE_TOKEN" -H "Content-Type: application/json" --data '{"value":0}'
# purge stale entries
curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" -H "Authorization: Bearer $CF_CACHE_TOKEN" -H "Content-Type: application/json" --data '{"purge_everything":true}'
```

Verify with `curl -D-` of an `/xrpc/` query: `cache-control` should read
`max-age=60`, not `14400`. (`Cache Purge` is a **Zone** permission — it doesn't
appear if the token's permission dropdown is scoped to Account.)

## Snapshot builder: R2 channels (don't clobber prod `latest.json`)

The indexer (`MODE=indexer`) writes the snapshot + manifest to R2 and a
pointer the search service polls. **Production search polls `latest.json`.**
A scratch/staging build must NOT overwrite it, or prod will poll an
incompatible build and reject it forever (masking real promotions).

`INDEX_CHANNEL` picks the publish target and **defaults to `local` (no
upload)**, so a build that forgets to declare a channel — or a one-off
machine launched without per-run env — can never clobber prod. Read by
`services/src/index/builder.zig`:

| `INDEX_CHANNEL` | publishes to | also requires | use |
|---|---|---|---|
| `local` (default) | nothing — build stays on the volume | — | pure inspection; read counts in the build dir |
| `staging` | `staging/<name>/<id>/` + `latest.<name>.json` | `INDEX_STAGING_NAME` (non-empty) | a scratch search instance can poll the namespaced latest |
| `prod` | `builds/<id>/` + `latest.json` | `INDEX_ALLOW_PROD=1` | the prod promote — deliberate, separate go/no-go |

- **Inspection**: omit `INDEX_CHANNEL` (or set `local`) → manifest + index.db
  land in the build dir; nothing is uploaded.
- **Staging**: `just indexer-build-staging <branch>` →
  `INDEX_CHANNEL=staging INDEX_STAGING_NAME=<branch>`. Prod's `latest.json` is untouched.
- **Production promote**: `INDEX_CHANNEL=prod INDEX_ALLOW_PROD=1`. Declaring
  `prod` alone is **not enough** — the arming flag is the second key. Without
  it the build refuses to publish and stays on disk for a retry.

## Recurring snapshot rebuilds (self-sustaining, 2026-06-15)

The overlay stays bounded only if the snapshot is rebuilt routinely (a 19-day
gap let `prefix_overlay` bloat to 7.9M rows → `q=bsky` 210s). This is now a
**fly daily-scheduled machine**, not a manual chore:

```sh
# build+push the current image
cd services && fly deploy -c fly.indexer.toml --build-only --push   # prints image ref

# one-time volume (≥4× snapshot; ~8.6GB snapshot → 50GB)
fly volumes create typeahead_indexer_data --size 50 --region iad -a typeahead-indexer -y

# daily scheduled prod build+promote (machine 'index-rebuild-daily')
fly machine run <image-ref> -a typeahead-indexer \
  --schedule daily --volume typeahead_indexer_data:/data \
  --vm-memory 2048 --vm-cpus 4 --vm-cpu-kind shared --region iad \
  --env MODE=indexer --env INDEX_CHANNEL=prod --env INDEX_ALLOW_PROD=1 --env INDEX_BUILD_ROOT=/data \
  --env INDEX_SAMPLE_LIMIT=0   # CRITICAL: 0 = full corpus. omitting it defaults to a 1000-actor
                               # SAMPLE build, which ActorCountFloor correctly rejects (never promotes).
```

App B (`typeahead-search`, `ENABLE_INDEX_PROMOTE=1`) polls `latest.json` and
auto-adopts the new build — so scheduled build + existing auto-promote = the
snapshot self-refreshes daily and the overlay stays ~1 day small. To run an
off-cycle rebuild now: `fly machine start <id of index-rebuild-daily>`. Do NOT
`fly deploy` this app as a service (loop-rebuilds); the scheduled machine is the
only thing that should run it. Remaining defense-in-depth (not yet done): cap the
overlay read in `overlayPrefixRows`, and alert on snapshot age / overlay size.

## service identity & lexicons (2026-07-25)

The service has its own atproto account: **@typeahead.waow.tech** =
`did:plc:y6kywgnb7xletjpa5hhxmd4k`, currently hosted on the bsky PDS
(migratable later; only the DID doc's service entry changes).

- handle verification: `_atproto.typeahead.waow.tech` TXT (CF zone waow.tech) +
  the worker serving `/.well-known/atproto-did` from the `SERVICE_DID` wrangler var.
- lexicon authority: `_lexicon.typeahead.waow.tech` TXT → the service DID; schema
  records live in its repo under `com.atproto.lexicon.schema` (rkey = NSID).
- to edit/add lexicons: change `lexicons/*.json`, then
  `ATP_PDS=https://bsky.social ATP_IDENTIFIER=typeahead.waow.tech ATP_PASSWORD=$ATPROTO_PASSWORD uv run scripts/publish-lexicon.py`
  (`ATPROTO_PASSWORD` is in the gitignored `.env`; source it, don't read it).
- endpoint changes must update `/docs`, `/llms.txt`, README.md, and the homepage
  API chip together; smoke asserts canonical/alias equivalence.

## freshness: what to check when someone says "it's not showing up" (2026-07-31)

`GET /health/freshness` — public, no token, 503 when anything is wrong:

```
{"ok":true,"stale":false,"replica_lag_s":37,"snapshot_age_s":19357,
 "build_id":"...","probe_ok":true,"reasons":[]}
```

It reports overlay lag (`replica_lag_s`, the field name predates the replica's
removal), snapshot age, and a live query through the real serving path. Check it **before** Logfire or fly logs — it needs neither, which matters
because the search service exported no telemetry at all until 2026-07-31.

Thresholds: overlay lag warns at 10 min, stale at 30 min (sync runs every 5 min,
edge cache is 60s). Snapshot stale at 4 days against a 3-day rebuild cadence.

The three layers and how to check each:

| layer | check | healthy |
|---|---|---|
| relay → Turso | `fly logs -a typeahead-ingester --no-tail` | `+N actors ... pending: 0`, cursor advancing |
| Turso → overlay | `/health/freshness` → `replica_lag_s` | < 600s |
| snapshot | `/index/status` → `build_id` | age < 3 days |

If the overlay is behind and no `sync.*` spans are arriving, the sync loop is
wedged: the watchdog exits the process within 20 min so fly restarts it. A
manual `fly machine restart -a typeahead-search` is safe — on boot the service
re-adopts the on-disk snapshot and resumes projecting from the persisted
watermark (`sync_meta.last_sync`, advanced per page); the worker's backstop
covers the seconds in between.

## how a request-indexing lands (2026-07-31)

Measured 1.3–6.4s end to end, of which 80–86% is the single-row Turso UPSERT
contending with the live ingester. Searchable ~0.16s after that write returns.

The worker POSTs the row it just wrote to the search service's
`/admin/refresh`, which projects it into the overlay immediately — the overlay
being the design's live-update channel. It
sends the **row**, not a DID: an earlier version had the search service re-read
the row from Turso, which put the fast path through the most contended
component in the system (measured: 28s for an indexed point lookup while a bulk
job ran).

Fire-and-forget. The 5-minute sync remains the mechanism of record, so a failed
push costs freshness for one actor, never correctness.

**Under firehose floods that write can queue behind the ingester's batch
transactions** — Turso is single-writer. Measured `turso_write;dur=10178`
(2026-08-04), with spikes past 45s; the smoke gate allows 60s for this check,
which is calibration, not a fix. The write stays synchronous on purpose: the
response says "indexed", and that has to be true (59f6e39). Moving it off the
response path reintroduces the untruthful-"indexed" failure mode; splitting the
response into "accepted, landing now" changes the UX contract. Revisit only if
user-facing timeouts actually appear — the endpoint's `Server-Timing` header
attributes the phases, so you'll be able to tell.

## /reputation: moderation status across labelers (2026-09-03)

`/reputation?actor=…` now answers "what do the moderation services say about
this account" next to "why does it rank here". Three facts shaped it:

- **the labels typeahead stores are partial by construction.** They come
  from the unauthenticated appview (`getProfiles`), which reports only
  self-labels and Bluesky's own moderation service. The page says so next to
  them (`storedLabelsSource`).
- **the appview cannot be asked for third-party labels without auth.**
  `atproto-accept-labelers` is ignored on `public.api.bsky.app` (verified: a
  Blacksky-labeled account came back label-free however the header was set).
  So the page asks each labeler directly on its own
  `com.atproto.label.queryLabels` endpoint, resolved from the labeler's DID
  document, 3 s timeout each, in parallel, per report. Nothing is stored;
  nothing touches the search path.
- **the labeler list is discovered, not typed.** `scripts/discover-labelers.py`
  searches the appview and keeps accounts it flags `associated.labeler`,
  filtered to moderation-type names with ≥ 50 subscribers, plus the appview
  operators' services pinned regardless (Bluesky, Blacksky, Northsky,
  Eurosky — Eurosky's has 2 subscribers and may not answer). Output is
  `src/labelers.json`; rerun the script to refresh, commit the diff.

Negations are resolved client-side (latest record per `val` wins, `neg`
withdraws, expired labels drop) — `applyLabelHistory` in `src/labelers.ts`.

The same change removed the page's two `COUNT(*)` queries over the visible
corpus (the last corpus-proportional read on a request path). The snapshot
builder now writes p0..p100 of `quality_score` into `index_meta`
(`score_quantiles`), `/index/status` exposes them, and the percentile is a
lookup in 101 numbers. Until the first build after 2026-09-03 promotes, the
page says "percentile unavailable".

## unattributed bulk clients: the 03:00 UTC spike (2026-09-03)

The daily spike in the `unknown` source on `/stats` is one `node` script on a
Vultr VPS (`45.63.115.226`, FR) that fires ~200 `searchActorsTypeahead`
requests with `limit=100` at 03:00 UTC, every night since 2026-08-17, all
Belgian vocabulary (`Wallonie`, `Eupen`, `chicon`, `asbl`, `guindaille`…) —
someone assembling a Belgian account list by keyword. No `X-Client`, `Origin`
or `Referer`, so it cannot be attributed. Found via Workers Logs grouped by
`cf-connecting-ip` + `user-agent` (the `/stats` counters have no per-client
detail by design).

What it cost: the engine was fine (179 searches, p50 7 ms). The worker was
not: a quarter of the burst hit the 60/min per-IP limiter, the rest ran 7–20 s
of wall time, and *other* users' median wall time went from 282 ms to 1.4 s
for those two minutes. Two reasons — every cache miss writes a stats row to
Turso in `waitUntil`, and every **empty** engine answer falls through to a
full-corpus Turso FTS query, which rare keywords trigger constantly.

**Decision (2026-09-03): the empty-result Turso fallback only runs for
`limit ≤ 25`** (`TURSO_FALLBACK_MAX_LIMIT` in `src/handlers/search.ts`).
A person typing a handle asks for ~10 rows and keeps the second look for an
actor the engine hasn't projected yet; a 100-row keyword sweep takes the
engine's answer as final. Nothing changes for browsers or for any client
using the default limit; the bot still gets answered. Not chosen: putting
unattributed clients on the strict limiter (would break real integrations
that never set headers) and leaving it (the fallback is the one
corpus-proportional read on the request path, and this let a stranger
schedule it). If a client with a legitimate need for `limit>25` plus the
Turso second look appears, it should identify itself with `X-Client` and we
can allowlist by app.

## observability: what the zig services emit (2026-09-02)

Both fly roles use [logfire-zig](https://tangled.org/zzstoatzz.io/logfire-zig)
≥ 0.3.0, wired the way its README prescribes and which other repos
(find-bufo next) should copy:

- `pub const std_options = .{ .logFn = logfire.logFn }` in `main.zig`: every
  `std.log.*` line ships to logfire as a log record carrying the current
  span's trace/span id, `code.module.name` (the std.log scope, e.g. `sync`),
  and `severity_text` — AND still reaches stderr, so `fly logs` is unchanged.
  Before this, logs were stderr-only and lost after ~100 lines.
- every span is published as a **pending span** at start, so a 10-minute
  `sync.incremental` pass is visible in the live view from second one.
- `span.setStatus(.@"error", …)` stamps `logfire.level_num = 17`; errored
  spans render red.
- `logfire.flush()` runs before every `std.process.exit` (both watchdogs, the
  firehose exit) and `pub const panic = std.debug.FullPanic(logfire.panicFn)`
  ships a panic's message. Without these the last 500 ms of telemetry — the
  interesting part — died with the process.
- resource: `service.name` = fly app, `service.version` = `FLY_IMAGE_REF`
  (the deployed image), `deployment.environment.name` = production.

Query shape: logs are `records` rows with `kind = 'log'`; join to spans on
`trace_id`. Pending spans have `attributes->>'logfire.span_type' = 'pending_span'`
and are replaced in the UI by the real span; filter them out of counts.

## alerting: overlaps and gaps (reviewed 2026-08-04)

Four systems page independently — logfire freshness, the typeahead freshness
bot, prefect crash automation, pub-search watchdog. Known state:

- **overlapping**: logfire and the typeahead bot both page on replica
  staleness, so one condition produces two pages.
- **uncovered**: enrichment queue depth, and the smoke gate's page-script
  check failing outside a deploy.

Fixed already: pub-search freshness threshold 30m→60m (overnight publish gaps),
cancel-≠-crash orchestration, the typeahead sync-stall class. The principle to
hold when adding one: **one page per signal, and every page actionable** — add
coverage only where a firing would change what you do.

# operations / edge gotchas

## Current automatic refresh — October 25, 2026

Public traffic uses the transport Worker's stable URL and
`control/search-routing.json`, currently selecting the admitted-startup backend.
The automatic-refresh deployment is active at 06:00 UTC on odd-numbered days,
with the next run queued for October 27. Production uses build
`b1792908655-14d5-px05` on `typeahead-search-refresh`; the previous managed backend
remains available for routing rollback. [The refresh runbook](plans/automatic-snapshot-refresh.md)
and [execution record](ops/automatic-refresh-20261025.md) cover verified rollout,
rollback, recovery, inventory and cleanup. The original compact app remains a
separately retained legacy rollback resource.
The dated October 20 configuration below is historical recovery context.

As of October 20, 2026, 16:31 UTC, production serves PDS-capable compact build
`b1792484789-3d65-px05` on `typeahead-search-compact` machine `8d4916be311008`,
following root `latest.json` at `/data/pds-current-20261020`, with live overlay
`/data/pds-overlay/overlay.db`. Worker `89901546-0e01-4789-b19a-541f0545460e`
keeps the homepage filter collapsed and sends search cache misses through the
private `typeahead-search-transport` binding placed in US East. Cache hits remain
at the public edge. [Latency evidence and rollback](evals/edge-latency-20261020.md)
record the direct-transport rollback version and deployment order.

The three non-serving search machines and their volumes were retired on October 24
(see below). The protected R2 rollback pointer and published images remain;
recovery now requires a fresh volume and machine. Disable scoped
search before routing to an incompatible backend; preserve current Worker source.
See [the PDS runbook](plans/pds-homepage-rollout.md) for the transition and recovery
inventory, and [release evidence](evals/pds-homepage-20261020.md) for full-corpus,
public API, browser and telemetry checks. The later
[parser cutover](evals/pds-parser-cutover-20261020.md) moved serving to
`86e502fe55e4e8` at 23:12 UTC, retaining `8d4916be311008` privately for PDS rollback.
At 23:27 UTC both bulk home schedules were verified inactive with no queued runs.
The previously queued October 22 publisher run will not execute; cloud publisher
and identity-repair qualification remain incomplete. Do not resume the old home
schedules before the cloud replacement passes its gates.
`services/fly.search-compact.toml` records the serving configuration. Plain search
deploys remain guarded in favor of the rollback-covered transition. Historical
restart and promotion instructions below refer to the legacy layout.

## Search machine sizing trial — October 24, 2026

The operator authorized testing two shared CPUs and 1 GiB RAM on serving machine
`86e502fe55e4e8`, down from four CPUs and 2 GiB. Only `config.guest` changed;
image, disk, environment, startup command and routing were verified unchanged.
The ingester remains one shared CPU and 256 MiB. The 60 GB search volume remains.

Before resizing, the available seven-day metrics showed search CPU averaging
0.013 cores, a five-minute peak of 0.172 cores, zero CPU throttling, and a
449 MiB peak for total memory minus available memory. That memory measure excludes
reclaimable cache. Index reloads and cache misses must still fit the smaller VM;
low steady-state usage alone is not a proof of startup capacity.

[Resize evidence](ops/search-resize-20261024.json) records before/after serial
native searches, readiness, overlay lag and post-ready observations. Roll back
resource sizing with:

```sh
fly machine update 86e502fe55e4e8 -a typeahead-search-compact --vm-cpus 4 --vm-memory 2048 --detach --yes
```

This restarts the sole search backend; verify readiness and overlay catch-up
before calling it recovered. The historical index pin and corpus are unchanged.

The 2 CPU / 1 GiB trial did not pass the cold-query checks. After attachment,
exact-handle, nate, alice and bob queries matched baseline DIDs; the next query
(`music`) exceeded the five-second client timeout. The automated check restored
4 CPUs / 2 GiB. This comparison used a warm original baseline and a cold restarted
candidate, so it does not establish that the size reduction alone caused the
failure. The smaller configuration was not retained.

During recovery, `EMERGENCY_PROXY_BSKY=1` temporarily routed unscoped search to
Bluesky's public API; PDS-scoped requests failed closed. The switch was removed
after original-size recovery and all 24 serial native requests passed with the
same DID results as baseline. Public exact-handle and `selfhosted.social` scoped
search both returned 200 through the canonical and compatibility aliases, with
byte-identical bodies within each pair. Public cache-miss timing included the
native origin again; the switch was also verified absent from Worker secrets.
Native recovery checks had a 100.00 ms median and 187.56 ms maximum round-trip
latency from the operator's Mac; these include network time and are not engine
benchmarks. This is a bounded recovery check, not a sustained-load qualification.

Cold startup spent most of its time waiting on file reads. `/proc/pressure/io`
showed about 95% I/O pressure; Fly reported zero CPU throttling and the 1 GiB VM
had substantial available memory. `FlatMap.open` validates and hashes all pages
of the 7.2 GB prefix file before attachment. `prepareSnapshot` loads the map once,
not once per read connection. A temporary 128 KiB to 1 MiB block read-ahead
experiment did not establish a benefit and was reverted to 128 KiB before rollback.
Investigate bounded startup/validation and cold-query latency before another
resource reduction; do not weaken integrity checks as a shortcut.

The subsequent [isolated startup experiment](evals/startup-buffer-20261024.md)
found no timing benefit from larger validation reads on a 100,000-actor subset.
Its metadata-only prototype became ready in 18 ms on the same verified fixture.
That prototype is restricted to the evaluation server and still needs an
admission/integrity protocol before production use. Production remains unchanged.
The subsequent [verified-startup candidate](evals/startup-admitted-candidate-20261024.md)
replaces the bypass with installation-time admission and per-block verification;
follow its qualification gates before changing routing.


## Search rollback resource retirement — October 24, 2026

The October 22 cost-reduction session stopped three non-serving machines but
retained their volumes for faster rollback. On October 24 the operator requested
removal of obsolete machines and disks. Removed:

| machine | app | deleted volume |
| --- | --- | --- |
| `867e15ce4e1148` | `typeahead-search` | `vol_4m3mkzpz1lmy383v` |
| `781e732c264678` | `typeahead-search-compact` | `vol_rnzg2g0gkko1lmpr` |
| `8d4916be311008` | `typeahead-search-compact` | `vol_4qlwyx1kl6o19x6r` |

Each volume was 60 GB. This removes $27/month of provisioned volume capacity.
The earlier compute savings were already achieved when the machines stopped;
do not count those again. Evergreen's trailing-seven-day compute estimate also
changes when deleted machines disappear from inventory; that is an estimate
change, not additional savings against the pre-deletion running inventory.
Deleted-volume snapshots can remain restorable until their retention expires;
no immediate snapshot billing reduction is claimed.

Production remains on `86e502fe55e4e8`, with volume `vol_vxmqwnpnlewdgzj4`.
The ingester remains on `7845636a3d0ee8`. Public exact-handle search passed before
cleanup and after each removal. The serving machine's configuration and generation
were identical before and after. [Evidence and non-secret recreation configurations](ops/search-retirement-20261024.json)
include the retired machines' pinned image digests and snapshot IDs.

Verified the protected rollback manifest via the serving machine's existing R2
client, and statted its 7,203,934,208-byte prefix object and 16,099,401,728-byte
legacy twin. This was an existence/size check, not a fresh full readback or restore
exercise. The protected pointer and objects remain unchanged.

Recovery now requires a new volume and machine rather than starting a retained
machine. While retained snapshots exist, use the saved snapshot IDs with
`fly volumes create --snapshot-id` to recreate a disk. Otherwise rebuild from
the protected R2 pointer. Use the saved PDS-capable rollback configuration for
`8d4916be311008`, replacing its mount with the newly created volume ID; retain
service exclusion until readiness and public/scoped verification pass. Existing
app secrets remain on `typeahead-search-compact`; no credential values are archived.
For a manual recovery, create a 60 GB volume in `ewr`, then extract the saved
`8d4916be311008` config from the evidence JSON, replace `mounts[0].volume` with
the new ID, and remove `mounts[0].name` and `mounts[0].size_gb`. Launch it with
`fly machine run <saved-image> -a typeahead-search-compact --machine-config <config.json>`.
Keep `services` empty while qualifying the replacement so it cannot receive
public traffic before readiness. Restore routing only after the existing scoped
search checks pass. Fly's installed CLI confirms `--machine-config` support;
this cleanup did not launch a replacement or re-run the full recovery drill.

The October 22 empty-volume recovery and watermark caveats below still apply.
Do not issue the old start/update commands against the deleted IDs.

### Historical stop procedure — October 22, 2026

To cut cost, every non-serving search machine is stopped; `86e502fe55e4e8` is the
only running search backend. Nothing was destroyed, and each stopped machine is
cordoned with service autostart off, so Fly's proxy cannot wake it.

| machine | app | role | state | kept |
| --- | --- | --- | --- | --- |
| `8d4916be311008` | `typeahead-search-compact` | PDS-capable rollback, pinned to `releases/pds-coverage-rollback-20261020/b1792484789-3d65-px05/latest.json`, image `pds-31ec142-20261020@sha256:7df51c4b…` | stopped, cordoned, autostart off | volume `vol_4qlwyx1kl6o19x6r` (60 GB) |
| `781e732c264678` | `typeahead-search-compact` | pre-PDS compact recovery (rollback group, no services) | stopped | its volume |
| `867e15ce4e1148` | `typeahead-search` | legacy backend; was not synchronized — every 5 min its promote failed with `UnsupportedManifestVersion` while re-downloading the current build | stopped, cordoned, autostart off | its volume and snapshots |

Cordon alone does not stop the proxy from autostarting a machine: the legacy box
was restarted by a request 10 s after it was stopped, until `--autostart=false`.

Rollback to `8d4916be311008` if `86e502fe55e4e8` fails:

1. If no backend is routed, flip the Worker to Bluesky's typeahead:
   `bunx wrangler secret put EMERGENCY_PROXY_BSKY` (delete it afterwards). Do not
   use `TURSO_FALLBACK_ON_UNREACHABLE`; it overloaded Turso on 2026-09-04. Without
   either, the Worker answers `{"actors":[]}` uncached.
2. `fly machine update 8d4916be311008 -a typeahead-search-compact --autostart=true --skip-start -y`
   then `fly machine start 8d4916be311008 -a typeahead-search-compact`.
3. Wait for readiness: the October 20 pin restart reached verified readiness 5m19s
   after launch; median compact startup is about 205 s.
4. Uncordon `8d4916be311008`, verify routing and public search, then cordon
   `86e502fe55e4e8`. Never remove the only healthy routed backend.

Overlay catch-up runs at about 2.5 min per hour stopped (September 2 replay of
19 h in 47 min; today's sync pages about 60–70 actors/s), so roughly 1 h per day
stopped. Route at readiness if needed: a stale overlay beats no results. Past
7 days stopped, `TOMBSTONE_RETENTION_S` means deletions before the watermark
return only with the next index promote.

If the volume were also lost, the R2 pin and its objects rebuild it: verified on
October 22, `releases/pds-candidate-31ec142/b1792484789-3d65-px05/index.db.prefix`
(7.20 GB) and `builds/b1792484789-3d65/index.db` (16.10 GB), with the rollback
image manifest still in `registry.fly.io`. A fresh 60 GB machine needs the
23.3 GB download plus admission (1,181.74 s in the October 19 empty restore),
about 25–45 min, then catch-up from the October 20 watermark.

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
edge cache is 60s). Snapshot stale at 4 days against a 3-day rebuild cadence. The compact rollout
adds age parsing for `compact-b<epoch>-...` IDs alongside legacy `b<epoch>-...`
IDs. This shipped October 17 in Worker version
`28622855-db7a-4cf6-9e18-762e3485f05a`, before compact backend cutover.

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

## the worker cron was being killed every hour (found 2026-09-03)

Workers Logs, 08-27..09-03: 140 scheduled runs, 120 ended `exceededWallTime`
(Cloudflare's 15-minute limit), 20 completed. CPU per run < 1 s; the rest was
waiting on the appview and Turso. Phase order was freshness → snapshot →
stats → refreshModeration (~8 min, ~1,000 rows) → enrichActors (identity
5,000 + profile 5,000 DIDs), so the last phase got ~7 minutes of a job sized
for 40, and the kill was invisible: not an exception, so `cron_step_failed`
never fired. Partial batches persisted, so it degraded rather than broke,
for weeks.

What changed:
- `enrichActors` is gone from the worker. Handles for new actors are
  `typeahead-identity-hourly` on heavypad (:20 past the hour, 6,000 DIDs,
  40-minute budget); profiles were already `typeahead-enrich-backfill` there
  (daily, 3.5 h, ~215k rows/run). The worker keeps freshness, stats, and the
  moderation refresh.
- the step loop has a 12-minute budget (`CRON_BUDGET_MS`): a step only starts
  with time left, and every run logs `cron_done` with per-phase milliseconds
  or `cron_budget_exhausted` naming what it skipped. Silence is no longer an
  outcome.
- the backfill's Turso calls retry with backoff (2 of 10 daily runs had died
  on one `ReadTimeout` from the shared single writer).

## profile sources: Bluesky, then Blacksky's appview, then the PDS (2026-09-03)

Bluesky's appview answers `AccountTakedown` for accounts it has suspended
and serves nothing about them. Blacksky's appview
(`api.blacksky.community`, `did:web:api.blacksky.community`) indexes the
whole network, answers without auth, and keeps serving those accounts,
counts included; the PDS record has name, avatar and description but no
counts. Enrichment (cron + request-indexing) now goes Bluesky → Blacksky →
PDS and writes which one won into `actors.profile_source`
(`bsky` | `blacksky` | `pds`; `''` = written before 2026-09-03).

Why the column exists: Blacksky's follower counts run lower than Bluesky's
for the same account (2,083 vs 2,531 for zzstoatzz.io; 88k vs 130k for
jcsalterego) because it indexes a narrower follow graph, so a
`quality_score` is only comparable with its source known. The report shows
it under "stored data" and on the ranking line. Policy unchanged: a Bluesky
takedown does not hide an account here (the PDS fallback always worked that
way); it just no longer zeroes its rank.

## the hidden count is sampling, not enforcement (2026-09-20)

`actors.hidden != 0` was 2,310 rows: 1,687 `spam`, 203 `!hide`, 410 from the
slur regex, domain blocks, or overrides, and 10 `!takedown`. Read against
Bluesky's daily enforcement volume that looks broken. It is not; it measures
something narrower:

- PDS-level suspensions and takedowns reach the relay as `#account` with
  `active: false`. The ingester deletes and tombstones them
  (`handleAccount` in `services/src/ingest.zig`). They never become hidden rows.
- Appview takedowns (getProfiles omits the DID) are re-enriched from Blacksky
  or the PDS record, whose labels never carry `!takedown`. By the policy above
  they stay visible; `resolveHidden` returns 0.
- Labels only arrive through getProfiles on the refresh cron, which walks the
  scored-visible pool (~41k rows, 800 per run) plus 200 firehose-touched rows.
  Nothing subscribes to the labeler stream. A `spam` label applied to a
  zero-score account after indexing is not observed until that account posts
  again.

So the number is a lower bound on labeled rows the cron has revisited. It was
dropped from the link-preview card that day and the `/stats` tile explains
itself in its tip. If an enforcement figure is ever wanted, count `#account`
deletes in a counter, or read `tombstones` (pruned after 7 days); do not
present `hidden` as one. `profile_source` has no index; grouping on it
full-scans 12M rows (a 60 s diagnostic timeout on 2026-09-20).

The card's replacement tile is `traffic_sources_total`: `materializeStats`
counts `traffic_sources` rows under `TRAFFIC_SOURCE_WHERE` (`src/metrics.ts`,
shared with the `/stats` list) minus the `unknown` and `homepage` attribution
buckets, and writes it into `stats_data`. First materialized 07:02 UTC
2026-09-20 at 203. Before that field exists the card counts distinct app
domains in `traffic_daily`, which undercounts (165 that morning) and never
overcounts.

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

**Decision (2026-09-04): the empty-result Turso fallback is OFF unless the
worker has `TURSO_FALLBACK_ON_EMPTY=1`**, and even then only for `limit ≤ 25`.
During the 2026-09-04 viral burst (~20x engine volume) the fallback alone put
Turso over its connection limit — 1,310 worker requests failed with
"Database connections limit exceeded" and 967 more timed out in 90 minutes —
and the ingester's `/admin/ingest` writes failed behind it until its backlog
overflowed and dropped events. Serving stayed 2xx throughout; discovery did
not. The fallback's reason (a lagging replica) has been gone since the
2026-09-02 replica removal: request-indexing pushes the row into the search
box's overlay synchronously. Turso still answers when the engine is
unreachable; it no longer answers when the engine says "no such actor".

**Same day, second decision: Turso-on-unreachable is also opt-in
(`TURSO_FALLBACK_ON_UNREACHABLE=1`), off by default.** Twenty minutes after
the first fix the search box panicked and restarted (15 s to adopt its
on-disk snapshot). In those 15 s every query went to the full-corpus Turso
path, Turso hit its connection limit again (119 errors, 83 timeouts), and
the ingester overflowed its backlog again. The engine is one machine; while
it is down the worker answers `{actors: []}` uncached and the next keystroke
reaches the restarted engine. Regression test: `index.test.ts` "an
unreachable engine answers empty without touching Turso".

The earlier decision it replaces (2026-09-03): the fallback only ran for
`limit ≤ 25` (`TURSO_FALLBACK_MAX_LIMIT` in `src/handlers/search.ts`).
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
- **the `search` span is head-sampled 1 in 10** (`search_span_sample_rate`
  in `index_search.zig`), stamped `sample_rate=10` so `count(*) * sample_rate`
  is still the volume. Decided 2026-09-04: during the viral burst the search
  box shipped 19k records in one hour, 99% of them this span at p50 3 ms,
  duplicating what `request_buckets` already counts. Baseline was ~10–20k
  records a *day*; a sustained burst would have been ~14M a month. Searches
  at or above `slow_search_ms` (100) log a warning regardless of sampling, so
  the tail is never lost; errors log regardless too. Volume check:
  `SELECT date_trunc('day', start_timestamp), count(*) FROM records GROUP BY 1`.
- `span.setStatus(.@"error", …)` stamps `logfire.level_num = 17`; errored
  spans render red.
- every fatal path goes through `services/src/fatal.zig` (2026-09-09):
  `fatal.exitForRestart()` for both watchdogs, the firehose exit, handler init
  and cursor failure; `pub const panic = std.debug.FullPanic(fatal.panicHook)`.
  Each arms a raw-thread deadman BEFORE flushing, then flushes, then exits.
  The flush ships the last 500 ms of telemetry when the exporter is healthy;
  the deadman (5 s exit / 3 s panic) guarantees the exit when it is not.
  otel-zig's `forceFlush` ignores its timeout at its lock and condvar waits,
  so a panic on the export thread otherwise hangs the process forever — it did
  so on 09-03 and again on 09-09 (see `docs/retros/`). A deadman firing prints
  `fatal: telemetry flush did not finish within N ms` to stderr.
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

## Search transport timing (October 20, 2026)

Search responses expose `Server-Timing`: `cache` identifies hit/miss and lookup
time; misses also expose `origin_headers`, `origin`, and `search` durations in
milliseconds. These phases overlap; do not add them. `origin_headers` carries
the Fly request ID, whose suffix identifies ingress, not the serving machine.
Compare it with `CF-Ray` and compact Logfire search spans when diagnosing
transport latency. Cached responses replace old timings with the current hit
lookup duration. See [the investigation](evals/edge-latency-20261020.md).


## SQL response failure handling (qualified October 20, 2026)

The native sync client must distinguish a successful empty query from an HTTP-200
Hrana statement error or malformed result. The parser fix rejects invalid
responses so incremental sync cannot advance the completed scope policy after
an unsuccessful query. The qualified image in the current production status
above predates this fix; source qualification is not deployment. See
[replay failure qualification](evals/pds-replay-response-fix.md) for evidence.

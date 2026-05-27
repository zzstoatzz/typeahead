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
keeps showing in the UI long after the DB + App B are correct; `curl` of a
cache-busted URL returns the fresh value but the cached query URL does not.

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

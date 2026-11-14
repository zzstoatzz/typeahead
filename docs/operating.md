# Operating typeahead

Procedures for the running system. [How it works](how-it-works.md) explains the
parts; read it first.

## What runs

| part | where | name |
| --- | --- | --- |
| public Worker | Cloudflare | `typeahead`, at `typeahead.waow.tech` |
| search transport | Cloudflare | `typeahead-search-transport` |
| image renderer | Cloudflare | `typeahead-image-renderer`, no public route |
| route record | R2 | `control/search-routing.json` in the `typeahead-index` bucket |
| control database | D1 | `typeahead-control` |
| statistics | D1 and KV | `typeahead-stats`, and the `stats_data` and `request_data` keys |
| two slots | Fly, `ewr` | `typeahead-shadow-a` and `typeahead-shadow-b` |

Each slot is one machine with four shared CPUs, 2 GB of memory and a 40 GB
volume at `/data`. One slot serves and the other stands by; the route record
says which. Nothing fails over by itself: every route change is a person
running a command.

## Look first

All of these only read.

```sh
curl -sS https://typeahead.waow.tech/health/freshness
curl -sS https://typeahead.waow.tech/health/control
curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route
./scripts/route/slot_route.py standby
just index-status
```

- `/health/freshness` asks the serving slot whether it is ready, how far its
  index is behind its store (`index_lag_s`), and whether a search for a known
  handle finds it (`probe_ok`). `ok: true` and `stale: false` is healthy.
- `/health/control` says whether the control database answers, and its policy
  revision.
- `/route` names the serving slot's origin, build and binary.
- `standby` runs the whole admission on the slot that is not serving. It exits
  0 and prints `"fit": true` when a flip to it would be allowed right now.

Per slot, with `<app>` as `typeahead-shadow-a` or `typeahead-shadow-b`:

```sh
curl -sS https://<app>.fly.dev/ready
curl -sS https://<app>.fly.dev/index/status
fly checks list -a <app>
fly logs -a <app> --no-tail
fly ssh console -a <app> -C "curl -s localhost:8082/health"
```

The search engine listens on 8080, which is the slot's public port. The store
process listens on 8082 and has no public service, so its `/health` is read
from the machine. [Store health](#store-health) lists what it reports.

To see which slot answered a public search, read the response headers. Change
`limit` if `server-timing` says the answer came from the cache.

```sh
curl -sS -D - -o /dev/null "https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=zzstoatzz.io&limit=7" | grep -i -E '^(x-typeahead|server-timing)'
```

## Upgrade the slots

Both slots run one image. To change it: deploy to the standby, check it, flip
the route, then deploy to the slot that was serving.

1. Find the standby.

   ```sh
   ./scripts/route/slot_route.py standby
   ```

2. Deploy to it, from the repository root.

   ```sh
   fly deploy . --config deploy/slot/fly.toml -a <standby app>
   ```

   The deploy restarts the machine. See [restarts](#restarts-and-warm-up).

3. Read the search engine's hash from the file on the machine. The slot also
   reports a hash about itself; this is the second source.

   ```sh
   fly ssh console -a <standby app> -C "sha256sum /app/typeahead-tantivy"
   ```

4. Wait until the standby is fit with that hash.

   ```sh
   ./scripts/route/slot_route.py standby --binary-sha256 <hash>
   ```

5. Run a [latency round](#restarts-and-warm-up) against the standby's own
   origin and wait for a clean one. Fit is not fast.
6. [Flip the route](#flip-the-route) to it.
7. Deploy the same image to the other slot and wait for `standby` to report it
   fit.

An image that changes the normalizer version, the plan version or the index
format cannot be admitted against a serving slot on the old one. The admission
compares all three, and there is no procedure for that case yet.

An image that changes what an index document holds needs every document
rewritten once. Pass `-e INDEX_RESYNC_AFTER=$(date +%s)` with the deploy to the
standby. The store then walks every account into the index in one round, and
the admission refuses the slot until the round completes.

A round is throttled at the slot's normal size: about 578 documents a second
on four shared CPUs, against 1,650 to 2,000 on four performance CPUs, so
several hours against 87 minutes (measured 2026-11-13). Give the standby
performance CPUs for the pass and put it back afterwards:

```sh
fly machine update <machine id> -a <standby app> --vm-cpu-kind performance --vm-cpus 4 --vm-memory 8192
fly machine update <machine id> -a <standby app> --vm-cpu-kind shared --vm-cpus 4 --vm-memory 2048
```

`fly deploy` sets a machine back to the size in `fly.toml`.
`fly machine update --image … --env …` changes the image or environment and
keeps the size.

After a round the search engine merges the rewritten segments for hours. At
the shared size slot A stayed 50 to 70% CPU-throttled, with a p90 of 165 to
343 ms, for more than an hour after its pass, and was clean the next day (2%
throttled, p90 42 to 45 ms). Do not flip to a freshly resynced slot at the
shared size until a [latency round](#restarts-and-warm-up) against it is
clean.

## Flip the route

A flip is one conditional write to the route record. No Worker is deployed and
no machine is touched.

The write needs R2 credentials in the environment: `INDEX_R2_ENDPOINT`,
`INDEX_R2_BUCKET`, `INDEX_R2_ACCESS_KEY_ID` and `INDEX_R2_SECRET_ACCESS_KEY`.

```sh
./scripts/route/slot_route.py cutover --origin https://<app>.fly.dev --binary-sha256 <hash>
./scripts/route/slot_route.py cutover --origin https://<app>.fly.dev --binary-sha256 <hash> --write
```

The first command prints the plan and writes nothing. Read `route.current`
(the slot that would serve), `route.previous` (the slot serving now) and
`admission.gates`. The second writes.

The slot is admitted when all of these hold:

- `/ready`, `/health` and `/index/status` answer 200 and agree on engine,
  build, format, binary and retrieval strategy.
- The binary hash equals the one passed in.
- Format, normalizer version, plan version and PDS scope equal the serving
  slot's.
- Moderation policy is complete, at most 300 s old, and equal to the serving
  slot's.
- The index is at most 120 s behind its store.
- A full round of the store into the index has completed and none is running.
- The account count is within 2% of the serving slot's.
- The serving slot is ready, and the candidate is not the serving slot.

With `--write` the tool stores the admission at
`control/store-slot-admissions/<sha256>.json`, writes the record only if it
has not changed since it was read, then asks the transport until it names the
new slot and a search acknowledges it. If the transport has not followed
within 60 s the tool puts the old record back and exits 1.

The transport re-reads the record within a second. The public Worker caches
each answer for 60 seconds, so a query asked in the last minute keeps its old
answer until then.

Afterwards run the checks under [look first](#look-first) and `just smoke`.

## Roll back

```sh
./scripts/route/slot_route.py rollback
./scripts/route/slot_route.py rollback --write
```

The first prints the record it would restore. Read it before writing: a
rollback returns to the record's `previous`, which is the slot that was serving
when the last flip was written.

A rollback restores the whole record the last flip replaced, from that flip's
stored admission. It checks only that the slot it returns to is ready and
still has the build and binary the record names. It does not compare the two
slots, so it works when the serving slot is broken. When there is time, a
[flip](#flip-the-route) to the other slot checks everything.

A rollback is refused, with the record unchanged, when `previous` names a slot
that no longer answers as the record describes it. That is the case when
`previous` is one of the stopped `typeahead-tantivy-*` slots, or a slot that
has been redeployed since. Use a flip, or a [failover](#fail-over), then.

## Fail over

When the serving slot answers 503 or does not answer, and the other slot is
healthy.

1. Confirm the serving slot is the problem.

   ```sh
   curl -sS -o /dev/null -w '%{http_code}\n' https://<serving app>.fly.dev/ready
   ./scripts/route/slot_route.py standby
   ```

   `standby` should print `"serving_healthy": false` and, for the other slot,
   `"fit": true` with `"cutover_needs_serving_slot_unhealthy": true`. If the
   other slot is not fit, do not fail over to it. Repair the serving slot.

2. Read the standby's hash from its machine, as in the upgrade.
3. Flip with the override.

   ```sh
   ./scripts/route/slot_route.py cutover --origin https://<standby app>.fly.dev --binary-sha256 <hash> --serving-slot-unhealthy
   ./scripts/route/slot_route.py cutover --origin https://<standby app>.fly.dev --binary-sha256 <hash> --serving-slot-unhealthy --write
   ```

The override is refused when the serving slot is in fact ready. Every check on
the standby itself still applies. The comparisons with the serving slot are
skipped when that slot cannot answer them, and the stored admission records
`compared_with_serving: false`. The defence is a `standby` report that was
green before the failure and a standby whose image has not changed since.

Nothing is copied between slots. Each has followed the network itself.

Until someone flips, unscoped searches are answered by Bluesky's public API
and searches with a `pds` filter answer 503. See
[when search is down](#when-search-is-down).

There is no need to fail back. Either slot can serve.

## Restarts and warm-up

`fly machine restart <machine id> -a <app>` restarts a slot. So does every
deploy and every secret set without `--stage`.

Restart the standby freely. Restart the serving slot only after flipping away
from it:

- The search engine is down until its new process is ready.
- A slot reports ready long before it answers at normal speed. At 2 GB it
  takes about 10 minutes to read its index files back into memory. Measured
  2026-11-13 on slot B: 4 minutes after a restart a round of 741 queries had
  66 failures and a p99 of 2.2 s; at 12 minutes it had none, with p50 34 ms,
  p90 41 ms and p99 59 ms.
- The index stops answering when it has heard nothing from the store for 120
  seconds. After a machine start the store took 22 to 42 s to reach its first
  push, and the index's lag peaked at 35 to 60 s.

`standby` printing `"fit": true` does not measure latency. Before flipping to
a slot that has restarted, run a latency round against its own origin and
require a clean one: no failures, and percentiles like the serving slot's.

```sh
just bench-search --url https://<app>.fly.dev
```

The supervisor inside a slot restarts either process by itself and leaves the
other running. The machine does not restart when a process exits.

| what failed | search on that slot | what shows it |
| --- | --- | --- |
| the store exits once | unaffected | `[supervisor] store exited` in `fly logs`, then `started store` |
| the store cannot stay up | answers 503 once 120 s pass without a push | `store_health` failing in `fly checks list`; `sync_lag_s` rising in `/index/status` |
| the search engine exits | down until the new process is ready | `[supervisor] index exited`; `index_ready` failing |
| the search engine cannot stay up | down | the same, repeating at most every 30 s |

In the last three cases, [fail over](#fail-over) if the slot is serving.

## Resize

Machine size is the `[[vm]]` block in `deploy/slot/fly.toml`, and
every `fly deploy` applies it. To resize without a deploy:

```sh
fly machine update <machine id> -a <app> --vm-cpu-kind shared --vm-cpus 4 --vm-memory 2048
```

Change `fly.toml` to match, or the next deploy undoes it. A resize restarts
the machine, so resize the standby, [check its latency](#restarts-and-warm-up),
flip, then resize the other.

A volume can grow while the machine runs and cannot shrink:

```sh
fly volumes list -a <app>
fly volumes extend <volume id> -s <GB> -a <app>
```


## Replace a lost volume

Fly snapshots each slot's volume daily and keeps two days of them. A slot
whose volume is lost or damaged is rebuilt from a snapshot, its own or the
other slot's: both hold the same accounts and index. This has not been
exercised yet; treat the first run as a rehearsal on the standby.

```sh
fly volumes snapshots list <volume id>
fly volumes create shadow_data --snapshot-id <snapshot id> -s 40 -r ewr -a <app>
```

Then destroy the slot's machine and [deploy](#upgrade-the-slots) to that app,
which creates a machine on the new volume. A volume restored from the other
slot's snapshot carries that slot's index cursor; `INDEX_ADOPT_CURSOR=1` in
`fly.toml` makes the bridge take the index over with one full resync round. The store resumes from the cursors
in the snapshot, so a snapshot older than the relay keeps events for leaves a
gap that the PLC directory and the scheduled refresh close over time; prefer
the newest snapshot. Wait for `standby` to report the slot fit and for a
clean latency round before flipping to it.

## Moderation

An account is hidden from search by a moderation label, a blocked handle
domain or an operator override. Overrides and blocked domains are the two an
operator sets. Both are rows in the control database, and every slot pulls the
whole policy from the Worker every 30 seconds. A change is in force in search
within about a minute. The public cache holds an answer for 60 seconds more.

The admin endpoints take the Worker's admin secret, which `.env` holds as
`ADMIN_SECRET`. Load it into the shell first:

```sh
set -a; . ./.env; set +a
```

`just smoke` reads the same secret from `TYPEAHEAD_SECRET`, so run it as
`TYPEAHEAD_SECRET="$ADMIN_SECRET" just smoke`. Without it the smoke suite
skips its admin checks.

```sh
# list
curl -sS -H "Authorization: Bearer $ADMIN_SECRET" https://typeahead.waow.tech/admin/mod-overrides
curl -sS -H "Authorization: Bearer $ADMIN_SECRET" https://typeahead.waow.tech/admin/domain-blocks

# hide or show one account, whatever its labels say
curl -sS -X POST -H "Authorization: Bearer $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"did":"did:plc:…","action":"hide","reason":"…"}' https://typeahead.waow.tech/admin/mod-override
curl -sS -X DELETE -H "Authorization: Bearer $ADMIN_SECRET" 'https://typeahead.waow.tech/admin/mod-override?did=did:plc:…'

# hide every handle under a domain
curl -sS -X POST -H "Authorization: Bearer $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"domain":"example.com","reason":"…"}' https://typeahead.waow.tech/admin/domain-block
curl -sS -X DELETE -H "Authorization: Bearer $ADMIN_SECRET" 'https://typeahead.waow.tech/admin/domain-block?domain=example.com'
```

`action` is `hide` or `show`. To confirm a change reached a slot, ask the
slot's own origin, which has no cache:

```sh
curl -sS 'https://<app>.fly.dev/search?q=<handle>&limit=5'
```

Write rules through these endpoints. They check that a slot can hold the rule
and raise the policy revision in the same transaction. A slot refuses a whole
policy over one rule it cannot hold, and slots that already have the current
revision do not ask again. A row changed by hand in D1 skips both.

One override is permanent: a `show` that keeps a taken-down account visible.
`just smoke` fails if it is missing and never writes it.

When the control database is down, the admin endpoints answer 503 and change
nothing, and each slot keeps the policy it has.

## Index requests

`POST /request-indexing` is the homepage's "index this account" button. The
Worker resolves and verifies the identity, then appends the DID to the control
database. Each slot takes new requests on its next poll and treats each one as
it treats an identity event from the relay: it creates the account if it is
new, resolves its handle and hosting server itself, and fetches its profile.

- A repeat for the same account within five minutes adds nothing.
- A slot takes at most 20 requests a poll, and none while 100 identity
  lookups are already waiting. Requests it does not take stay at the Worker,
  in order.
- The hourly cron removes requests older than seven days.
- A request does not bring back an account the relay reported deactivated,
  deleted or taken down. The Worker still answers 200 for it.
- When the control database is down the Worker still answers, but the request
  reaches no slot and nothing replays it.

## The control database

`typeahead-control` holds four tables: `mod_overrides`, `blocked_domains`,
`index_requests` and `control_meta`. `control_meta` has the policy revision,
which rises with every policy change, and a generation, a random number that
identifies this database. Slots read it through `GET /slot/control/policy` and
`GET /slot/control/index-requests` with `SLOT_CONTROL_SECRET`, a secret that
can read these two and change nothing.

A slot's revision and request position are positions in one database. If the
database is recreated, its migration seeds a new generation and slots start
over by themselves. If it is restored to an earlier time with D1 Time Travel,
it comes back with its old generation, so give it a new one straight away:

```sh
bunx wrangler d1 execute typeahead-control --remote --command \
  "UPDATE control_meta SET value = abs(random() % 9007199254740991) + 1 WHERE name = 'generation'"
```

Until that runs, slots refuse the restored policy as older than theirs and
miss requests logged under positions they have passed.

Migrations are in `migrations/control-d1`:
`bunx wrangler d1 migrations apply typeahead-control --remote`.

## Deploy the Workers

```sh
just deploy-worker            # public Worker, then the API and image smoke suites
just deploy-search-transport  # transport, then the API smoke suite
just deploy-image-renderer    # renderer, then the image smoke suite
```

Push before deploying. All three lint and type-check first.
`just deploy-worker-only` skips the smoke suites.

`just deploy-worker` and `just deploy-worker-only` run `just control-ready`
first. It refuses to deploy while `wrangler.jsonc` has the placeholder
`database_id` for `CONTROL_DB`, or while the control database lacks its
schema, which it reads through wrangler. Without the schema every admin
policy change fails and no indexing request is logged.

Deploy the renderer before a public Worker that changes how it calls it. The
transport accepts only the slot origins listed in `src/search-route.ts`; a
new slot's origin has to be deployed there before a record can name it.

The public Worker's settings are in `wrangler.jsonc`. Profiles, the
reputation page and the account counts on `/stats` are read from the serving
slot through the transport. Policy and index requests are in the
`typeahead-control` D1 database and statistics in `typeahead-stats`. The
retired `/admin` write endpoints answer 410.

## Statistics

`/stats` shows searches, apps, answers by endpoint and status, and account
counts. `/stats?view=status` is the first place to look when something seems
wrong.

- Every answer the Worker gives is counted by app, endpoint and status, in
  5-minute buckets. Searches that miss the cache are also counted by app.
- The hourly cron writes the page's series to KV, records the account counts
  it reads from the serving slot, and once a day records how many accounts
  moderation hides. Each run logs `cron_done` with the time each step took.
- Statistics are written to and read from the `typeahead-stats` D1 database.
  A failed write is logged as `stats_write_failed` and does not fail the
  request.
- Traffic sent with `X-Client: smoke-testing` stays in the totals and is left
  out of the app list.

```sh
bunx wrangler d1 execute typeahead-stats --remote --command "SELECT datetime(bucket * 300, 'unixepoch') AS bucket_utc, sum(hits) AS answers FROM request_buckets GROUP BY bucket ORDER BY bucket DESC LIMIT 6"
```

Migrations are in `migrations/stats-d1`. Worker logs are in Cloudflare's
Workers Logs for `typeahead`: `worker_request_timing` per request,
`cron_done` and `cron_step_failed` per cron run.

Every response carries `Server-Timing` with `worker`, and for searches
`search`, `cache` and `origin`. Compare a client's time after TLS with
`worker`, then `worker` with `search`, to find where a slow request spent its
time.

## Alerts

Alerts come from three places.

The Worker's hourly cron posts to the alert webhook (`ALERT_WEBHOOK_URL`, a
Discord channel) when a state changes, not every hour:

| message | meaning | do |
| --- | --- | --- |
| serving is stale | the serving slot's index is more than 30 minutes behind its store; the slot stopped answering searches at 2 minutes | check the slot's store; [fail over](#fail-over) if the standby is fit |
| serving is fresh again | the above has cleared | nothing |
| the control database is not answering | `typeahead-control` failed the hourly probe | policy changes fail and index requests reach no slot until it answers; search is unaffected |

`/health/freshness` also turns `ok: false` when the serving slot is
unreachable, not ready, or does not find the probe handle. Those do not post
by themselves, so read the endpoint when in doubt.

Logfire, project `typeahead`, has one alert per slot:

| alert | meaning | do |
| --- | --- | --- |
| store slot heartbeat missing or relay stalled | no `store.heartbeat` from that slot for 15 minutes, or its newest relay event is over 15 minutes old | read `fly logs` and the store's `/health`; if the slot is serving and does not recover, [fail over](#fail-over) |
| telemetry volume exceeds 50,000 records in 15 minutes | something is logging in a loop | find the source and stop it |

Evergreen's fleet-health sweep (repository `evergreen`,
`site/services.json`) reads `/health/freshness` every 15 minutes and posts to
the Prefect alerts channel when it fails. Treat it as the freshness alert
above.

The store process sends one `store.heartbeat` span a minute. Its attributes
include `control_age_seconds`, the time since the slot last reached the
Worker for policy. Above 300, a policy change made now is not in force on that
slot; search is unaffected.

## When search is down

Unscoped searches fall back to Bluesky's public API by themselves when the
serving slot fails, times out or answers badly. Those answers are not cached.
Searches with a `pds` filter answer 503, because Bluesky cannot honor the
filter.

To send every unscoped search to Bluesky by hand:

```sh
bunx wrangler secret put EMERGENCY_PROXY_BSKY      # any value; on
bunx wrangler secret delete EMERGENCY_PROXY_BSKY   # off
```

Restore search first, with a [failover](#fail-over) or this switch, and
diagnose afterwards.

## Edge configuration

`/xrpc/` requests have to work for API clients that are not browsers. Keep the
Cloudflare Browser Integrity Check exception for `/xrpc/`, and keep the zone's
cache TTL on Respect Existing Headers so the Worker's 60-second lifetime
holds.

## Store health

`/health` on port 8082 answers `"role":"store"` with these blocks. The numbers
under `store` and `queues` are refreshed once a minute.

| block | holds |
| --- | --- |
| `store` | cursors for the relay, labeler, PLC directory and index bridge; the change log's head and floor; bytes on disk; last commit |
| `queues` | identity lookups and profile refreshes waiting (`identity_due`, `refresh_due`) |
| `enrichment` | the profile refresh loop: pacing, completed, retried, not found, rate-limited, and any pause in force |
| `control` | the policy revision held, when the Worker last confirmed it, the last index request taken, failures by kind, and `age_seconds` |
| `corpus` | the last count of accounts: live, with handles, with avatars, hidden |
| `first_check` | a one-time pass that queues a first profile fetch for accounts no source has answered for; done when `finished_at` is set |

For the first 30 seconds after the store opens, the counts read zero and
`/store/*` answers 503. The account count first appears 300 seconds in.

A `control` block whose `state` is `misconfigured` means the slot has no
usable `SLOT_CONTROL_SECRET`. The slot serves with the policy it has.
Settings are listed in `services/src/store_process/config.zig` and
`deploy/slot/README.md`.

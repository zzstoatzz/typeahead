# cutover to the store-fed slot

how to route search to `typeahead-shadow-a`, the slot fed by its own store's change log, and how to route back. nothing here has been done: as of 2026-11-11 the routing record names `typeahead-tantivy-b`, and the code this runbook needs is on the `route-shadow` branch, not deployed. it also assumes the standby refresh in my-prefect-server, which on that date is on its `typeahead-standby-refresh` branch, not merged and not deployed.

every command is marked. **verified** means it was run on 2026-11-11 and behaved as described. **unverified** means it was not run, because it writes, needs credentials this was written without, or touches a deployed account.

## what moves and what does not

the public Worker asks the search transport Worker, which reads the routing record `control/search-routing.json` in the `typeahead-index` R2 bucket and forwards to the origin it names. the transport accepts only origins in a closed list in [`src/search-route.ts`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/search-route.ts), and refuses any answer whose `X-Typeahead-Build`, `-Format`, `-Binary-Sha256` or `-Retrieval-Strategy` header differs from the record. a cutover is one conditional write of that record. no Worker is deployed and no machine is touched during it.

the refresh controller in [my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/tantivy-slot-lifecycle.md) is the only thing that has written the record until now, and it names only `typeahead-tantivy-a` and `-b`. it cannot admit the store slot: its admission comes from a bundle it installed, a Fly machine shape it created and a release receipt bound to both. [`scripts/snapshot_refresh/store_slot_route.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/snapshot_refresh/store_slot_route.py) admits the store slot instead, on what the slot reports compared with the slot serving now:

- `/ready`, `/health` and `/index/status` all answer 200 and ready, and each acknowledges the same engine, build, format, binary and retrieval strategy
- the binary sha256 equals the one you pass in, which you read from the machine, not from the slot's own status
- format, normalizer version, plan version and PDS scope policy equal the serving slot's
- moderation policy is enabled, complete and fresh (at most 300 s old), its canonical rules hash equals the serving slot's, and its policy hash equals the serving slot's when that slot's policy is fresh
- sync lag is at most 120 s, the reader refuses partial or stale answers, and its own lag bound is at most 120 s
- the source cursor is `store-change-log-v1`, names the index it serves, is between cycles, and has applied observations
- a reconciliation round against the store has completed since the bootstrap bundle's cut, and none is running
- it holds at least half the serving slot's actors

it does not read `cutover_source_ready`. the reader computes that flag only for the `turso-authoritative-observations-v2` policy, and it is `false` on the store slot and, today, on both production slots too. the tool also does not judge search quality: that is the [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md), which must have passed first.

## before the day

these two deploys change nothing for traffic while the record names a managed slot. the transport change adds one origin to the list it will accept. the public Worker change only alters how `/health/freshness` measures a backend whose source policy is `store-change-log-v1`.

```sh
just check
just deploy-search-transport
just deploy-worker
```

**unverified**: neither deploy was run. `just check` was run on the branch and passed.

without the transport deploy the cutover fails safe: the transport cannot parse the record, the tool sees `/route` answer 503 and puts the prior record back at once. unscoped searches fall back to Bluesky during those seconds and PDS-scoped searches answer 503. without the public Worker deploy, `/health/freshness` alerts "refresh missed" 15 h after the store slot's last reconciliation round, "stale" at 24 h, and "stale" again four days after its bootstrap, with nothing wrong.

to confirm the transport deploy landed (**unverified**):

```sh
bunx wrangler deployments list -c deploy/search-transport/wrangler.jsonc
```

## pre-flight

all read-only.

1. the route as the transport sees it. **verified**: it named `typeahead-tantivy-b`, build `b1794377098-cp-17abf2a622`.

   ```sh
   curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route | jq .
   ```

2. the store slot. **verified**: ready, build `b1794333857-cp-e3e8808936`, sync lag under 30 s, `source_policy` `store-change-log-v1`, `cutover_source_ready` `false`.

   ```sh
   curl -sS https://typeahead-shadow-a.fly.dev/ready
   curl -sS https://typeahead-shadow-a.fly.dev/health
   curl -sS https://typeahead-shadow-a.fly.dev/index/status | jq '{build_id, binary_sha256, ready, sync_lag_s, cutover_source_ready, normalize_version, plan_version, policy: .source_cursor.source_policy, round: .source_cursor.reconciliation, moderation: (.moderation | {fresh, age_s, sha256, canonical_rules_source_sha256})}'
   ```

3. the reader's hash, from the file on the machine. **unverified**. on 2026-11-11 the slot itself reported `43cef99a99d53ceb994134b4a00693d845484735e49e7c0e6cb5f82ca0a6d529`; the point of this step is a second source.

   ```sh
   fly ssh console -a typeahead-shadow-a -C "sha256sum /app/typeahead-tantivy"
   ```

4. the admission, without credentials or writes. **verified**: exit 0 with the hash above, and a refusal listing each mismatch with the production reader's hash. it prints `"refused": [...]` and exits 1 when any gate fails.

   ```sh
   ./scripts/snapshot_refresh/store_slot_route.py check --binary-sha256 <hash from step 3>
   ```

5. no refresh is running or about to. **verified**: both commands ran; the schedule is `0 6,18 * * *` UTC, id `ae561c16-cb18-4291-be09-2e7f370d276f`, and nothing was running.

   ```sh
   cd ~/tangled.org/zzstoatzz.io/my-prefect-server
   just prefect --no-prompt deployment schedule ls typeahead-index/typeahead-index
   just prefect --no-prompt flow-run ls --state-type RUNNING --state-type PENDING
   ```

   the tool also refuses on its own while the record carries a refresh's mutation lease, and its write is conditional on the record's ETag, so a refresh that promotes in between wins and the cutover reports that nothing was written.

6. the public freshness report, for comparison afterwards. **verified**.

   ```sh
   curl -sS https://typeahead.waow.tech/health/freshness | jq .
   ```

## the refresh while the store slot is routed

with the standby refresh deployed in my-prefect-server, leave the twice-daily schedule running. do not pause it. while the record's `current` is the store slot, the scheduled run becomes a standby refresh ([automatic typeahead refresh](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/automatic-typeahead-refresh.md), section "standby refresh while a store slot serves"; read, not run):

- it exports, builds and publishes as usual, then installs and qualifies the new generation on the managed slot that is not the record's `previous`
- it stores the record a rollback should restore, `{current: the rebuilt slot, previous: the other managed slot}`, at `control/tantivy-standby/<store slot admission sha256>/<rebuilt slot admission sha256>.json`
- it replaces only `previous` in the routing record, with one write conditional on the ETag. `current` is copied byte for byte, so it never moves the route while the store slot is routed
- the next run rebuilds the other slot, so each of A and B is rebuilt once a day

a standby refresh that builds and records finishes as an ordinary completed run, so the missed-refresh automation, which expects a completed refresh every 13 hours, keeps working as before.

**unverified**: no standby refresh has run against production. the rollback tool was run against the ledger that branch's code writes, in memory (`just test-store-route`, and a scratch check that imported the branch's constants and encoder).

confirm the standby refresh is deployed before the cutover. without it the refresh fails at its first read of the record, before it exports, publishes, leases or touches a slot, retries once and fails again, twice a day, and the managed slots are not rebuilt. it does not flip the route back. in that case only, pause the schedule:

```sh
just prefect --no-prompt deployment schedule pause typeahead-index/typeahead-index ae561c16-cb18-4291-be09-2e7f370d276f
```

**unverified**: the pause was not run; `deployment schedule pause --help` confirms the argument order. also unverified: whether registering deployments again from `prefect.heavypad.yaml` re-activates the schedule. check `schedule ls` after any such registration.

## cutover

the write needs the R2 credentials the refresh controller uses, in the environment: `INDEX_R2_ENDPOINT`, `INDEX_R2_BUCKET`, `INDEX_R2_ACCESS_KEY_ID`, `INDEX_R2_SECRET_ACCESS_KEY`. **unverified**: this was written without them, so no command in this section or the rollback section has run against R2. the logic was exercised against an in-memory bucket (`just test-store-route`), and the same boto3 conditional `put_object` is what the refresh controller uses against this bucket.

print the plan. this reads the record from R2, repeats the admission against the slot the record names, and writes nothing:

```sh
./scripts/snapshot_refresh/store_slot_route.py cutover --binary-sha256 <hash>
```

read `route.current` (the store slot's identity), `route.previous` (the slot serving now) and `admission.gates`. then:

```sh
./scripts/snapshot_refresh/store_slot_route.py cutover --binary-sha256 <hash> --write
```

with `--write` it:

1. stores the admission, which includes the record being replaced, at `control/store-slot-admissions/<admission_sha256>.json`, only if that key does not exist
2. writes `{version, current: store slot, previous: prior current}` to `control/search-routing.json`, only if the record still has the ETag it read
3. asks the transport for `/route` and for one search until the transport names the store slot and the search acknowledges its build, format, binary and strategy
4. puts the prior record back if the transport answers 503 for `/route`, or has not followed after 60 s, and exits 1 saying so

the transport re-reads the record at least every second. the public Worker caches each answer for 60 s per query, so a query asked in the last minute keeps its old answer, with the old slot's headers, until that entry expires.

## verify from outside

the serving slot is named by response headers that the public Worker copies from the backend: `x-typeahead-build`, `x-typeahead-binary-sha256` and `x-typeahead-machine-id`. on the store slot they are `b1794333857-cp-e3e8808936`, the hash from pre-flight, and `185d5e7b045118` (values read 2026-11-11; the build id stays the same for the life of the volume, the machine id for the life of the machine).

```sh
curl -sS -D - -o /dev/null "https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=zzstoatzz.io&limit=7" | grep -i -E '^(x-typeahead|server-timing)'
```

**verified** as a command; on 2026-11-11 it showed slot B (`b1794377098-cp-17abf2a622`, machine `2869394f4d1e58`). `server-timing: cache;desc="miss"` means the answer came from the backend just now. on a `hit`, change `limit`, which is part of the cache key, or wait 60 s. use a query that matches someone: when `TURSO_FALLBACK_ON_EMPTY` is set on the Worker, an empty answer is served from Turso and carries no backend headers.

```sh
curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route | jq -r .origin
curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/index/status | jq '{build_id, sync_lag_s, policy: .source_cursor.source_policy}'
curl -sS https://typeahead.waow.tech/health/freshness | jq .
just smoke
```

**verified** as commands for the first three, which showed slot B and the Turso policy; `just smoke` was **not run**. after a cutover expect the origin `https://typeahead-shadow-a.fly.dev`, the policy `store-change-log-v1`, and a freshness report with `ok: true`, `probe_ok: true`, the store slot's `build_id`, and `snapshot_age_s`, `coverage_age_s` and `coverage_deadline` all `null`.

watch `sync_lag_s` over a few minutes. the bridge sends a watermark at least every 30 s by default, so a low lag shows the bridge and index are alive. it does not show that the store is still taking in new records: the store's own health is on the machine's private port 8082 and nothing outside reads it yet.

## if a process on the store slot fails while it is routed

the slot's supervisor restarts whichever process exited, in place, and leaves the other running ([slot README](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/README.md#what-a-failure-looks-like)). the machine does not restart and Fly does not stop it. **verified** against stand-in processes only (`just test-supervise`); **unverified** on the machine.

- a store restart does not interrupt search. the index serves on its last watermark for 120 s, and the wait before a store restart is at most 30 s.
- a store that cannot stay up long enough to push a watermark makes the slot report not ready 120 s after the last one: `/ready` answers 503 and so does search. `sync_lag_s` climbing past 30 is the early sign from outside. on the machine, `fly logs -a typeahead-shadow-a` shows `[supervisor] store stopped on its own, N time(s) without staying up 300s`, and `fly checks list -a typeahead-shadow-a` shows `store_health` failing while the store is not listening.
- an index restart is an outage of the slot until the new process is ready.

in the last two cases roll back, below. the rollback does not need the store slot to be healthy.

## rollback

```sh
./scripts/snapshot_refresh/store_slot_route.py rollback
./scripts/snapshot_refresh/store_slot_route.py rollback --write
```

**unverified** against R2. the first prints the record it would restore. the second:

1. reads the routed store slot's admission from `control/store-slot-admissions/` and checks its digest against the record
2. picks the record to restore. if the record's `previous` is still the slot that was serving at cutover, that is the prior record stored in the admission. if a standby refresh has repointed `previous`, it is the route in that refresh's ledger object under `control/tantivy-standby/`, which must name this store slot's admission. either way the restored `current` is the record's `previous`; anything else is refused
3. checks that this slot's `/ready` is true and that it still acknowledges the build, format, binary and strategy the record names
4. writes the restored record, `current` and `previous`, conditional on the ETag
5. verifies through the transport as the cutover does, and puts the store slot's record back if the transport does not follow

so a rollback before any standby refresh returns to the slot that was serving at cutover, and a rollback after one returns to the slot that refresh rebuilt, with the other managed slot as `previous`.

it does not need the store slot to be healthy. the exact record matters: the refresh controller refuses to prepare an inactive slot that still holds a machine unless the record's `previous` names that slot, so a record restored without `previous` would leave every later refresh failing.

then verify as above, expecting the build and machine id of the slot the record's `previous` named. the schedule needs no change: once the record names a managed slot again, the next scheduled run is an ordinary promoting refresh. a rollback that lands while a standby refresh is preparing or recording its slot has its own recovery steps in [automatic typeahead refresh](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/automatic-typeahead-refresh.md) (read, not run).

if the schedule was paused because the standby refresh was not deployed, resume it:

```sh
just prefect --no-prompt deployment schedule resume typeahead-index/typeahead-index ae561c16-cb18-4291-be09-2e7f370d276f
```

**unverified**; `deployment schedule resume --help` confirms the argument order.

if the rollback is refused because the slot it returns to is not ready or answers with another identity, the record is unchanged and the store slot keeps serving. bring that slot back first (`fly machine start` in its app, **unverified**) and run the rollback again. there is no tool for routing to the other managed slot by hand.

## what must keep running for rollback to stay possible

- **slots A and B.** do not stop, resize, redeploy or destroy `typeahead-tantivy-a` or `-b`, or their volumes, by hand. the record's `previous` is the rollback target and must keep the build and binary the record names. the other one is the slot the next standby refresh rebuilds.
- **the ingester and Turso.** each managed slot polls Turso from inside its machine. the ingester (`typeahead-ingester`), the Worker's admin ingest and Turso itself must keep running, or the managed slots stop following the network. a slot whose poll is more than 120 s behind reports not ready, and the rollback refuses it.
- **the twice-daily refresh.** it rebuilds A and B in turn and never moves the route while the store slot is routed. it is what keeps the rollback target's checkpoint coverage renewed; without it a slot keeps serving and polling past its coverage deadline (24 h after its checkpoint cut or last full round) and reports `coverage_stale`, and `/health/freshness` reports stale coverage after a rollback until a refresh completes.
- **the store slot's admission object** in `control/store-slot-admissions/` **and the standby ledger objects** in `control/tantivy-standby/`. between them they hold the record to restore.
- **no manual slot operation.** `tantivy_operation` requests, `cancel` included, are refused while the store slot is routed.

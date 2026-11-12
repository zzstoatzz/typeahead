# store slot operations

how to route search to a store slot (`typeahead-shadow-a` or `typeahead-shadow-b`, each fed by its own store's change log), how to see whether the one that is not serving could take over, how to fail over to it by hand, and how to route back.

status, 2026-11-12:

- the transport's `/route` names `typeahead-shadow-a`, build `b1794333857-cp-e3e8808936` (**verified** by reading it). the record has named it since the cutover on 2026-11-11.
- `typeahead-shadow-b` is being bootstrapped. its `/index/status` did not answer within 8 s (**verified**), so it is not a standby yet.
- the second-slot support in this document (the `typeahead-shadow-b` origin in the transport, store-to-store cutover, `standby`, `--serving-slot-unhealthy`) was written on this date and tested against an in-memory bucket and fixture slots (`just test-store-route`). it is **not deployed**, and no store-to-store cutover, failover or rollback has run against production.
- nothing here fails over on its own. every route change is a person running a command.

commands are marked. **verified** means it was run on the date given and behaved as described. **unverified** means it was not run, because it writes, needs credentials this was written without, or touches a deployed account.

## what moves and what does not

the public Worker asks the search transport Worker, which reads the routing record `control/search-routing.json` in the `typeahead-index` R2 bucket and forwards to the origin it names. the transport accepts only origins in a closed list in [`src/search-route.ts`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/search-route.ts), and refuses any answer whose `X-Typeahead-Build`, `-Format`, `-Binary-Sha256` or `-Retrieval-Strategy` header differs from the record. a cutover is one conditional write of that record. no Worker is deployed and no machine is touched during it.

the refresh controller in [my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/tantivy-slot-lifecycle.md) names only `typeahead-tantivy-a` and `-b`, the managed slots. it cannot admit a store slot: its admission comes from a bundle it installed, a Fly machine shape it created and a release receipt bound to both. [`scripts/snapshot_refresh/store_slot_route.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/snapshot_refresh/store_slot_route.py) admits a store slot instead, on what the slot reports compared with the slot serving now. the serving slot can be a managed slot or the other store slot. the slot to route to, the candidate, is always named with `--origin`; there is no default.

| command | reads | writes |
| --- | --- | --- |
| `standby [--binary-sha256 HEX]` | the transport's `/route`, both slots' public endpoints | nothing |
| `check --origin URL --binary-sha256 HEX` | the same | nothing |
| `cutover --origin URL --binary-sha256 HEX` | the record in R2, both slots | nothing: prints the plan |
| `cutover ... --write` | the same | the admission object, then the record |
| `rollback` | the record and the routed slot's admission in R2, the slot to return to | nothing: prints the plan |
| `rollback --write` | the same | the record |

`check` and `cutover` also take `--serving-slot-unhealthy`, described under [failover](#failover-from-an-unhealthy-serving-slot).

## admission gates

a candidate is admitted when all of these hold:

- its `/ready`, `/health` and `/index/status` all answer 200 and ready, and each acknowledges the same engine, build, format, binary and retrieval strategy
- the binary sha256 equals the one you pass in, which you read from the machine, not from the slot's own status
- format, normalizer version, plan version and PDS scope version and policy equal the serving slot's
- moderation policy is enabled, complete and fresh (at most 300 s old), its canonical rules hash equals the serving slot's, and its policy hash equals the serving slot's when that slot's policy is fresh
- sync lag is at most 120 s, the reader refuses partial or stale answers, and its own lag bound is at most 120 s
- the source cursor is `store-change-log-v1`, names the index it serves, is between cycles, and has applied observations
- a reconciliation round against its store has completed since its bootstrap bundle's cut, and none is running
- its actor count is within 2% of the serving slot's, either way
- the serving slot itself is ready and acknowledges the build, format, binary and strategy the record names
- it is not the slot that is serving

it does not read `cutover_source_ready`. the reader computes that flag only for the `turso-authoritative-observations-v2` policy, and it is `false` on a store slot. the tool also does not judge search quality: that is the [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md), which must have passed first. nothing in this change compares `typeahead-shadow-b`'s answers with anyone's.

## what `previous` means

`previous` in the record a cutover writes is the target that was serving when the cutover was written. it is the slot `rollback` returns to. it is not "the standby": the standby is whichever store slot is not `current`, and `standby` finds it from the list of store origins, not from the record.

`rollback` restores the whole record the cutover replaced, `current` and `previous`, from the admission object stored for the routed slot at `control/store-slot-admissions/<admission_sha256>.json`. so each rollback undoes exactly one cutover:

an example, starting from a record that names two managed slots:

| step | record after it |
| --- | --- |
| start | `current` managed B, `previous` managed A |
| `cutover --origin` shadow-a | `current` shadow-a, `previous` managed B |
| `cutover --origin` shadow-b | `current` shadow-b, `previous` shadow-a |
| `cutover --origin` shadow-a | `current` shadow-a (a new admission), `previous` shadow-b |
| `rollback` | `current` shadow-b, `previous` shadow-a |
| `rollback` | `current` shadow-a (the first admission), `previous` managed B |
| `rollback` | `current` managed B, `previous` managed A |

this was chosen over swapping `current` and `previous` because the restored record is one that was admitted, written and served before, byte for byte, and because it leaves the managed-slot case exactly as it was: the refresh controller needs the record it wrote back, `previous` included. the cost is that after a rollback `previous` may name a slot further back than the one just left, as in the last two rows. to route to a particular slot, use `cutover --origin`, which works whatever `previous` says.

two consequences today:

- the record's `previous` should still be the managed slot the 2026-11-11 cutover replaced. it was not read: this was written without R2 credentials, and the transport's `/route` shows only `current`. `typeahead-shadow-b` becomes `previous` only after it has served. until then `rollback` returns to the managed slot, and routing to `typeahead-shadow-b` is a `cutover`.
- once the managed slots are retired, a rollback that would return to one is refused, because the slot no longer answers ready as the record names it. the record is left as it is.

in every case the restored `current` must equal the routed record's `previous`; anything else is refused. the one exception to "the record the cutover replaced" is the refresh controller's standby refresh, which repoints `previous` to a managed slot it rebuilt and stores the route to restore under `control/tantivy-standby/`; see [rollback](#rollback). that ledger is consulted only when `previous` is a managed slot.

## deploying the transport with the second origin

the transport has to hold `https://typeahead-shadow-b.fly.dev` in its list before a record can name it. only the transport Worker bundles the list (`src/search-transport.ts` and the route authority it creates); the public Worker does not import it.

```sh
just check
just deploy-search-transport
bunx wrangler deployments list -c deploy/search-transport/wrangler.jsonc
```

**unverified**: the deploy was not run. `just --no-dotenv check` was run on the branch on 2026-11-12 and passed.

this deploy changes nothing for traffic. the change to the Worker is one more accepted value for `origin`. a record that names `typeahead-shadow-a` parses to the same route as before, `previous` is never read, and a test holds that (`either store slot is routable, and the record's previous never changes the route` in `src/search-transport.test.ts`).

without this deploy a cutover to `typeahead-shadow-b` fails safe: the transport cannot parse the record, the tool sees `/route` answer 503 and puts the prior record back at once. unscoped searches fall back to Bluesky during those seconds and PDS-scoped searches answer 503.

## standby health

```sh
./scripts/snapshot_refresh/store_slot_route.py standby
```

**verified** 2026-11-12 against production: it named `typeahead-shadow-a` as serving and healthy, reported `typeahead-shadow-b` not fit because its `/index/status` timed out, and exited 1.

it reads the transport's `/route`, then runs the cutover's whole admission on each store slot that is not serving. it needs no credentials and writes nothing. read:

- `fit`: whether a cutover to the standby would be admitted right now. the exit status is 0 when it is and 1 when it is not, or when the transport cannot name the route
- `serving_healthy` and `serving_unhealthy_reasons`: whether the serving slot is ready as the slot the record names
- `standby[].refused`: every gate the standby fails
- `standby[].cutover_needs_serving_slot_unhealthy`: `true` when the serving slot is not healthy, so the standby was judged as a failover and the cutover would need the override
- `standby[].gates`: the measurements, including `sync_lag_s`, `reconciliation_age_s`, `policy_sha256` and both actor counts

without `--binary-sha256` the standby is expected to run the binary the serving store slot's record names. that hash was read from the serving slot's machine at its admission, so this is a check that both slots run the same reader. it is still the standby's own word for which binary it runs; only the hash read from its machine before a cutover is a second source. pass `--binary-sha256` when the standby is meant to run a different reader, as during an upgrade. while a managed slot is serving, the hash has to be passed, because that slot's reader is a different binary.

nothing runs this on a schedule yet.

## what must stay true for the second slot to be a valid standby

- **it answers and follows.** `standby` exits 0: ready, sync lag at most 120 s, a completed reconciliation round since its bootstrap and none running. a slot in the middle of a round is refused until the round ends.
- **the transport deployed has its origin.** see above.
- **the same reader, except during an upgrade.** the tool does not require the two slots to run one binary: it requires the candidate to run the binary whose hash you read from its machine, and the format, normalizer version, plan version and PDS scope to equal the serving slot's. the policy to keep is one image on both slots. to upgrade, deploy the new image to the standby, confirm `standby --binary-sha256 <new hash>`, cut over to it, then deploy the same image to the other slot. a new image that changes the normalizer, plan or format cannot be admitted against a serving slot on the old one; that needs its own plan.
- **equal moderation.** the canonical rules hash and the policy hash must equal the serving slot's. as of 2026-11-12 a change to the moderation policy does not reach a store slot; another change is addressing that. until it lands each slot keeps the policy it holds, so the hashes are equal only if both slots hold the same one. which policy `typeahead-shadow-b` was bootstrapped with was not checked. if `standby` reports "moderation policy differs", the standby stays refused until the slots hold one policy again, and the failover override does not waive it while the serving slot's policy is fresh.
- **actor counts within 2%.** two slots following the same network should differ by seconds of arrivals. a standby bootstrapped from a bundle and then reconciled against its own store may not match at first; the report shows both counts.
- **the admission objects** in `control/store-slot-admissions/` are kept. they hold the records rollbacks restore.
- **each slot keeps its volume and machine.** a store slot's build id is that of its bootstrap bundle and lasts as long as its volume. re-bootstrapping the standby is fine. re-bootstrapping or redeploying the slot the record names as `previous` changes the identity `rollback` checks, so the rollback is refused and the way back is a `cutover`.
- **the refresh controller knows the origin before any refresh runs with it routed.** in my-prefect-server's checkout on 2026-11-12 (commit `58e4440`) `STORE_SLOT_ORIGINS` in `packages/mps/src/mps/typeahead_refresh.py` lists only `typeahead-shadow-a`, and the twice-daily refresh was paused by commit `d2f3412`. what a refresh does with a record whose `current` is `typeahead-shadow-b`, or whose `previous` is a store slot, was not tested; the standby refresh's reader in that checkout requires `previous` to be a managed slot. add the origin there, and decide what the refresh does with a store-slot `previous`, before resuming the schedule while either holds. whether the pause is deployed was not checked.

## pre-flight

all read-only. `<app>` is the candidate's Fly app, `typeahead-shadow-a` or `typeahead-shadow-b`. the results recorded here are from 2026-11-11, before the first cutover, with `typeahead-shadow-a` as the candidate.

1. the route as the transport sees it. **verified** 2026-11-11: it named `typeahead-tantivy-b`, build `b1794377098-cp-17abf2a622`. **verified** 2026-11-12: it named `typeahead-shadow-a`.

   ```sh
   curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route | jq .
   ```

2. the candidate. **verified** 2026-11-11 for `typeahead-shadow-a`: ready, build `b1794333857-cp-e3e8808936`, sync lag under 30 s, `source_policy` `store-change-log-v1`, `cutover_source_ready` `false`.

   ```sh
   curl -sS https://<app>.fly.dev/ready
   curl -sS https://<app>.fly.dev/health
   curl -sS https://<app>.fly.dev/index/status | jq '{build_id, binary_sha256, ready, sync_lag_s, cutover_source_ready, normalize_version, plan_version, policy: .source_cursor.source_policy, round: .source_cursor.reconciliation, moderation: (.moderation | {fresh, age_s, sha256, canonical_rules_source_sha256})}'
   ```

3. the reader's hash, from the file on the machine. **unverified**. the point of this step is a second source: the slot's own status and headers also report a hash (on 2026-11-12 `typeahead-shadow-a` reported `dc019d76912df63b6f1a6dc2feec24c8e0078110f80d4009e0481113096f4339`, which is also what the record names).

   ```sh
   fly ssh console -a <app> -C "sha256sum /app/typeahead-tantivy"
   ```

4. the admission, without credentials or writes. it prints `"refused": [...]` and exits 1 when any gate fails. **verified** 2026-11-11 with `typeahead-shadow-a` against the managed slot: exit 0 with the slot's hash, and a refusal listing each mismatch with the production reader's hash. **verified** 2026-11-12: naming `typeahead-shadow-a`, which is serving, is refused as "already routed". **unverified** for `typeahead-shadow-b`, which does not answer yet.

   ```sh
   ./scripts/snapshot_refresh/store_slot_route.py check --origin https://<app>.fly.dev --binary-sha256 <hash from step 3>
   ```

5. no refresh is running or about to. **verified** 2026-11-11: both commands ran; the schedule is `0 6,18 * * *` UTC, id `ae561c16-cb18-4291-be09-2e7f370d276f`, and nothing was running.

   ```sh
   cd ~/tangled.org/zzstoatzz.io/my-prefect-server
   just prefect --no-prompt deployment schedule ls typeahead-index/typeahead-index
   just prefect --no-prompt flow-run ls --state-type RUNNING --state-type PENDING
   ```

   the tool also refuses on its own while the record carries a refresh's mutation lease, and its write is conditional on the record's ETag, so a refresh that promotes in between wins and the cutover reports that nothing was written.

6. the public freshness report, for comparison afterwards. **verified** 2026-11-11.

   ```sh
   curl -sS https://typeahead.waow.tech/health/freshness | jq .
   ```

## the refresh while a store slot is routed

this section describes the managed slots' twice-daily refresh while `typeahead-shadow-a` is `current` and a managed slot is `previous`. my-prefect-server's commit `d2f3412` (2026-11-12) pauses that schedule and its missed-run automation; whether that is deployed was not checked here. the section does not cover a record whose `current` is `typeahead-shadow-b` or whose `previous` is a store slot: see the last item under [what must stay true](#what-must-stay-true-for-the-second-slot-to-be-a-valid-standby).

with the standby refresh deployed in my-prefect-server, leave the twice-daily schedule running. do not pause it. while the record's `current` is the store slot, the scheduled run becomes a standby refresh ([automatic typeahead refresh](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/automatic-typeahead-refresh.md), section "standby refresh while a store slot serves"; read, not run):

- it exports, builds and publishes as usual, then installs and qualifies the new generation on the managed slot that is not the record's `previous`
- it stores the record a rollback should restore, `{current: the rebuilt slot, previous: the other managed slot}`, at `control/tantivy-standby/<store slot admission sha256>/<rebuilt slot admission sha256>.json`
- it replaces only `previous` in the routing record, with one write conditional on the ETag. `current` is copied byte for byte, so it never moves the route while the store slot is routed
- the next run rebuilds the other slot, so each of A and B is rebuilt once a day

a standby refresh that builds and records finishes as an ordinary completed run, so the missed-refresh automation, which expects a completed refresh every 13 hours, keeps working as before.

**unverified** as of 2026-11-11: no standby refresh had run against production. the rollback tool was run against the ledger that branch's code writes, in memory (`just test-store-route`, and a scratch check that imported the branch's constants and encoder).

confirm the standby refresh is deployed before the cutover. without it the refresh fails at its first read of the record, before it exports, publishes, leases or touches a slot, retries once and fails again, twice a day, and the managed slots are not rebuilt. it does not flip the route back. in that case only, pause the schedule:

```sh
just prefect --no-prompt deployment schedule pause typeahead-index/typeahead-index ae561c16-cb18-4291-be09-2e7f370d276f
```

**unverified**: the pause was not run; `deployment schedule pause --help` confirms the argument order. also unverified: whether registering deployments again from `prefect.heavypad.yaml` re-activates the schedule. check `schedule ls` after any such registration.

## cutover

the write needs the R2 credentials the refresh controller uses, in the environment: `INDEX_R2_ENDPOINT`, `INDEX_R2_BUCKET`, `INDEX_R2_ACCESS_KEY_ID`, `INDEX_R2_SECRET_ACCESS_KEY`. this document was written without them. the cutover to `typeahead-shadow-a` on 2026-11-11 was this tool writing to R2 from a managed slot. **unverified** from one store slot to the other, which has run only against an in-memory bucket (`just test-store-route`).

the same two commands route to either store slot, from a managed slot or from the other store slot. to route to `typeahead-shadow-b`:

```sh
./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-b.fly.dev --binary-sha256 <hash>
```

this prints the plan. it reads the record from R2, repeats the admission against the slot the record names, and writes nothing. read `route.current` (the candidate's identity), `route.previous` (the slot serving now) and `admission.gates`. then:

```sh
./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-b.fly.dev --binary-sha256 <hash> --write
```

with `--write` it:

1. stores the admission, which includes the record being replaced, at `control/store-slot-admissions/<admission_sha256>.json`, only if that key does not exist
2. writes `{version, current: candidate, previous: prior current}` to `control/search-routing.json`, only if the record still has the ETag it read
3. asks the transport for `/route` and for one search until the transport names the candidate and the search acknowledges its build, format, binary and strategy
4. puts the prior record back if the transport answers 503 for `/route`, or has not followed after 60 s, and exits 1 saying so

it refuses, writing nothing, when the candidate is the slot already serving, when the record carries a refresh's mutation lease, and when the serving slot is not ready. the last is what [failover](#failover-from-an-unhealthy-serving-slot) is for.

the transport re-reads the record at least every second. the public Worker caches each answer for 60 s per query, so a query asked in the last minute keeps its old answer, with the old slot's headers, until that entry expires.

## verify from outside

the serving slot is named by response headers that the public Worker copies from the backend: `x-typeahead-build`, `x-typeahead-binary-sha256` and `x-typeahead-machine-id`. on `typeahead-shadow-a` they are `b1794333857-cp-e3e8808936`, `dc019d76912df63b6f1a6dc2feec24c8e0078110f80d4009e0481113096f4339` and `185d5e7b045118` (read from its `/index/status` 2026-11-12; the build id stays the same for the life of the volume, the machine id for the life of the machine). `typeahead-shadow-b`'s were not read: it does not answer yet. take them from the cutover's plan (`route.current.build_id`, `admission.gates.machine_id`).

```sh
curl -sS -D - -o /dev/null "https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=zzstoatzz.io&limit=7" | grep -i -E '^(x-typeahead|server-timing)'
```

**verified** as a command; on 2026-11-11 it showed managed slot B (`b1794377098-cp-17abf2a622`, machine `2869394f4d1e58`). `server-timing: cache;desc="miss"` means the answer came from the backend just now. on a `hit`, change `limit`, which is part of the cache key, or wait 60 s. use a query that matches someone: when `TURSO_FALLBACK_ON_EMPTY` is set on the Worker, an empty answer is served from Turso and carries no backend headers.

```sh
curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route | jq -r .origin
curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/index/status | jq '{build_id, sync_lag_s, policy: .source_cursor.source_policy}'
curl -sS https://typeahead.waow.tech/health/freshness | jq .
just smoke
```

**verified** 2026-11-11 as commands for the first three, which showed managed slot B and the Turso policy; `just smoke` was **not run**. after a cutover expect the origin of the slot routed to, the policy `store-change-log-v1`, and a freshness report with `ok: true`, `probe_ok: true`, the store slot's `build_id`, and `snapshot_age_s`, `coverage_age_s` and `coverage_deadline` all `null`.

watch `sync_lag_s` over a few minutes. the bridge sends a watermark at least every 30 s by default, so a low lag shows the bridge and index are alive. it does not show that the store is still taking in new records: the store's own health is on the machine's private port 8082 and nothing outside reads it yet.

## if a process on the routed store slot fails

the slot's supervisor restarts whichever process exited, in place, and leaves the other running ([slot README](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/README.md#what-a-failure-looks-like)). the machine does not restart and Fly does not stop it. **verified** against stand-in processes only (`just test-supervise`); **unverified** on the machine.

- a store restart does not interrupt search. the index serves on its last watermark for 120 s, and the wait before a store restart is at most 30 s.
- a store that cannot stay up long enough to push a watermark makes the slot report not ready 120 s after the last one: `/ready` answers 503 and so does search. `sync_lag_s` climbing past 30 is the early sign from outside. on the machine, `fly logs -a typeahead-shadow-a` shows `[supervisor] store stopped on its own, N time(s) without staying up 300s`, and `fly checks list -a typeahead-shadow-a` shows `store_health` failing while the store is not listening.
- an index restart is an outage of the slot until the new process is ready.

in the last two cases route away: [fail over](#failover-from-an-unhealthy-serving-slot) to the other store slot if `standby` says it is fit, or [roll back](#rollback) to the record's `previous`. neither needs the routed slot to be healthy.

## failover from an unhealthy serving slot

by hand, when the serving store slot answers 503 or does not answer and the other store slot is fit. the example fails over from `typeahead-shadow-a` to `typeahead-shadow-b`; swap the names for the other direction. **unverified** against production throughout: it has run only against fixture slots.

an ordinary `check` or `cutover` refuses here, because it judges the candidate against the serving slot and that slot is not ready. `--serving-slot-unhealthy` is the override for this case and nothing else:

- it is refused when the serving slot is in fact ready as the slot the record names, so it cannot be used to skip a comparison on an ordinary day
- every gate on the candidate itself still applies: ready on all three endpoints, the binary hash you pass, sync lag, the store cursor, a completed reconciliation round, fresh moderation, a non-zero actor count
- when the serving slot's `/index/status` still answers as the slot the record names (the fixture slots do while `/ready` is 503; what a real slot answers in each kind of failure was not observed), the comparisons are made as usual: normalizer, plan and PDS scope versions, moderation rules hash, actor count within 2%, and the moderation policy hash if the serving slot's policy is fresh
- when it does not answer, or answers as another build or binary, those comparisons are skipped

what protection is lost when they are skipped: nothing checks that the candidate normalizes, plans and scopes queries the way the slot it replaces did, that it enforces the same moderation rules and policy, or that it holds about as many actors. a candidate that passes its own gates but was built from another image, holds another moderation policy, or is missing a tenth of the network would be routed. the admission stored in R2 records `compared_with_serving: false` and the reasons the serving slot was judged unhealthy. the defence is a `standby` report that was green while the serving slot was still healthy, and a slot whose image and policy have not been changed since.

the steps:

1. confirm from outside that the serving slot is the problem, not the transport or the public Worker.

   ```sh
   curl -sS -o /dev/null -w '%{http_code}\n' https://typeahead-shadow-a.fly.dev/ready
   curl -sS https://typeahead-search-transport.n8-3e9.workers.dev/route | jq -r .origin
   ./scripts/snapshot_refresh/store_slot_route.py standby
   ```

   `standby` should show `serving_healthy: false` with the reasons, and the other slot with `fit: true` and `cutover_needs_serving_slot_unhealthy: true`. if the other slot is not fit, do not fail over to it: [roll back](#rollback) to `previous` if that slot is ready, or repair the serving slot.

2. read the candidate's reader hash from its machine.

   ```sh
   fly ssh console -a typeahead-shadow-b -C "sha256sum /app/typeahead-tantivy"
   ```

3. print the plan, read `admission.gates` (`compared_with_serving`, `serving_unhealthy_reasons`), then write.

   ```sh
   ./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-b.fly.dev --binary-sha256 <hash> --serving-slot-unhealthy
   ./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-b.fly.dev --binary-sha256 <hash> --serving-slot-unhealthy --write
   ```

4. [verify from outside](#verify-from-outside), expecting the candidate's origin, build and machine id.

the write, the transport check and the restore are the cutover's. if the transport does not follow, the prior record is put back, which names the unhealthy slot again; the tool says so and exits 1. the record's `previous` is now the slot that failed.

each store slot has its own store and follows the network itself. nothing is copied from the failed slot, and nothing it had that the standby lacks comes with the route.

## failing back

once the slot that failed answers again there are two ways to return to it. both are **unverified** against production.

- **a new cutover**, when there is no hurry. this is a full admission of the recovered slot against the one serving now, with the hash read from its machine again, and it leaves the other slot as `previous`. wait until `standby` reports it fit: a slot that was down has to catch up and may run a reconciliation round first.

  ```sh
  ./scripts/snapshot_refresh/store_slot_route.py standby
  ./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-a.fly.dev --binary-sha256 <hash>
  ./scripts/snapshot_refresh/store_slot_route.py cutover --origin https://typeahead-shadow-a.fly.dev --binary-sha256 <hash> --write
  ```

- **a rollback**, when the slot now serving is the one failing. it checks only that the slot it returns to answers ready with the build, format, binary and strategy the record names, and restores the record the failover replaced, so `previous` goes back to what it was before the failover.

there is no need to fail back. either store slot can serve, and the other is the standby.

## rollback

```sh
./scripts/snapshot_refresh/store_slot_route.py rollback
./scripts/snapshot_refresh/store_slot_route.py rollback --write
```

**unverified** against R2. the first prints the record it would restore. the second:

1. reads the routed store slot's admission from `control/store-slot-admissions/` and checks its digest against the record
2. picks the record to restore. if the record's `previous` is still the slot that was serving at cutover, managed or store, that is the prior record stored in the admission. if `previous` is a managed slot that a standby refresh has repointed, it is the route in that refresh's ledger object under `control/tantivy-standby/`, which must name this store slot's admission. either way the restored `current` is the record's `previous`; anything else is refused
3. checks that this slot's `/ready` is true and that it still acknowledges the build, format, binary and strategy the record names
4. writes the restored record, `current` and `previous`, conditional on the ETag
5. verifies through the transport as the cutover does, and puts the store slot's record back if the transport does not follow

so a rollback from one store slot returns to the other when the other was serving at cutover. a rollback to a managed slot before any standby refresh returns to the slot that was serving at cutover, and after one returns to the slot that refresh rebuilt, with the other managed slot as `previous`.

a rollback is a quick way back, not an admission. it does not compare the slot it returns to with anything, check its sync lag beyond what `/ready` implies, or look at its moderation policy. when the routed slot is healthy and there is time, a `cutover --origin` to the other slot checks all of that.

it does not need the routed store slot to be healthy. the exact record matters: the refresh controller refuses to prepare an inactive slot that still holds a machine unless the record's `previous` names that slot, so a record restored without `previous` would leave every later refresh failing.

then verify as above, expecting the build and machine id of the slot the record's `previous` named. after a rollback to a managed slot the schedule needs no change: once the record names a managed slot again, the next scheduled run is an ordinary promoting refresh. a rollback that lands while a standby refresh is preparing or recording its slot has its own recovery steps in [automatic typeahead refresh](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/automatic-typeahead-refresh.md) (read, not run).

if the schedule was paused because the standby refresh was not deployed, resume it:

```sh
just prefect --no-prompt deployment schedule resume typeahead-index/typeahead-index ae561c16-cb18-4291-be09-2e7f370d276f
```

**unverified**; `deployment schedule resume --help` confirms the argument order.

if the rollback is refused because the slot it returns to is not ready or answers with another identity, the record is unchanged and the store slot keeps serving. if the other store slot is fit, route to it with a `cutover`, as a [failover](#failover-from-an-unhealthy-serving-slot) if the routed slot is unhealthy. otherwise bring the `previous` slot back first (`fly machine start` in its app, **unverified**) and run the rollback again. there is no tool for routing to a managed slot other than `previous` by hand.

## what must keep running for a rollback to a managed slot to stay possible

this applies while the record's `previous` is a managed slot. retiring the managed slots gives this rollback up; the second store slot, admitted by `cutover`, is what replaces it.

- **slots A and B.** do not stop, resize, redeploy or destroy `typeahead-tantivy-a` or `-b`, or their volumes, by hand. the record's `previous` is the rollback target and must keep the build and binary the record names. the other one is the slot the next standby refresh rebuilds.
- **the ingester and Turso.** each managed slot polls Turso from inside its machine. the ingester (`typeahead-ingester`), the Worker's admin ingest and Turso itself must keep running, or the managed slots stop following the network. a slot whose poll is more than 120 s behind reports not ready, and the rollback refuses it.
- **the twice-daily refresh.** it rebuilds A and B in turn and never moves the route while the store slot is routed. it is what keeps the rollback target's checkpoint coverage renewed; without it a slot keeps serving and polling past its coverage deadline (24 h after its checkpoint cut or last full round) and reports `coverage_stale`, and `/health/freshness` reports stale coverage after a rollback until a refresh completes.
- **the store slot's admission object** in `control/store-slot-admissions/` **and the standby ledger objects** in `control/tantivy-standby/`. between them they hold the record to restore.
- **no manual slot operation.** `tantivy_operation` requests, `cancel` included, are refused while the store slot is routed.

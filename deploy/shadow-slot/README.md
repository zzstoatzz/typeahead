# Shadow slot

Packaging for one slot of the [embedded-store service](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md): a single Fly machine with a single volume that runs the store process and the Tantivy index process side by side. It is meant to run beside production with no route pointing at it, so its answers can be compared before it serves anyone.

Status, 2026-11-10: the Fly app `typeahead-shadow-a` exists with a 40 GB volume, and its index was bootstrapped on the machine from bundle `b1794333857-cp-e3e8808936` (13,164,304 actors). The store process is not running there yet, so the index is unfed and answers searches with 503: its moderation policy has to be refreshed every 300 seconds, which the store process will do. No route points at the app. The supervisor was also exercised locally; see [validation](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/validation.md).

## How it differs from a production slot

A production slot (`typeahead-tantivy-a` and `-b`) runs the Tantivy reader plus a poller that reads Turso, is installed from a published bundle by the refresh controller twice a day, and is sized at two shared CPUs, 1 GB of memory and a 20 GB volume. The ingester is a separate app.

The shadow slot puts the ingester's successor on the same machine as the reader. It holds no Turso credentials and no Worker admin secret, takes a bundle once at bootstrap, and afterwards is fed only by its own store. Its origin is in the transport's list of allowed origins in [`src/search-route.ts`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/search-route.ts), so the routing record can name it once a transport built from that list is deployed. The refresh controller never writes such a record; only [`store_slot_route.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/snapshot_refresh/store_slot_route.py) does, as the [cutover runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/ops/cutover-to-store-slot.md) describes.

The store process is a role of the ingester binary, selected with `MODE=store`, and is still being built. The binary in an image built from a tree without that role refuses the mode and exits, and the supervisor keeps retrying it while the index runs. The supervisor always starts the binary in that mode and refuses to start at all when Turso or Worker credentials are present, so this image cannot become a second production ingester by mistake.

## Build

From the repository root:

```sh
docker build --platform linux/amd64 -f deploy/shadow-slot/Dockerfile -t typeahead-shadow-slot:local .
```

The Zig compiler, the Rust toolchain and both base images are pinned by version and checksum. The Zig steps run with the network off from the vendored dependencies; Cargo fetches the locked crates in one step and compiles offline in the next. The reader built here is not byte-identical to the released production reader, so its `binary_sha256` differs.

## Processes

The supervisor starts both processes, prefixes each line of their output with `[store]` or `[index]`, and keeps both running. It looks at them once a second (`SHADOW_TICK_S`). A process that exits on its own is started again in place and the other is left alone. The supervisor itself exits only when it is asked to stop, so a process that keeps failing never takes the machine down with it and never uses up Fly's restart limit.

The wait before a restart is 1 s the first time and doubles to a cap of 30 s (`SHADOW_RESTART_MIN_S`, `SHADOW_RESTART_MAX_S`). It returns to 1 s once the process has stayed up 300 s (`SHADOW_RESTART_STABLE_S`). The cap is well under the 120 s the index serves without a watermark, so a store that runs long enough between exits to push one keeps search up.

Restarting one process alone is safe because the two share nothing but the index's loopback admin endpoints, and the [index bridge](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/index-bridge.md) keeps its progress in the index's own commit. A new store process reads the committed cursor and continues from it. A store process whose index went away fails its push, forgets what it knew, and reads the cursor again once the index answers. `just test-bridge` does both against a real index: it kills the bridge before or after a random request in each of sixty rounds, and the index just before and just after a commit.

A store process starts in the order `Stage` names in `services/src/store_process/role.zig`, because the index's watermark moves only when the bridge pushes. It opens the store for writing, reads the relay cursor, opens the bridge's read handle, then starts the owner, the bridge, and the relay, labeler, PLC and identity consumers. For 30 s after that it reads the store for nothing else: the owner leaves pruning, the label-expiry pass, change-log trimming, the handle repair and the health counts until then, `/store/*` answers 503 until its own handle is open, which starts when the 30 s are up and took 4 to 27 s more in the kills measured, and the first corpus count waits 300 s. `/health` answers throughout, with `store.taken_at` 0 and zero counts until the first refresh. The log has one line per stage (`start: bridge_started after 3301 ms`) and one for the first push the bridge commits. How long that leaves the index without a watermark, after a kill and after a machine start, is in [sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/sizing.md#what-a-store-restart-costs-the-watermark).

SIGTERM and SIGINT stop the store first, with SIGTERM, and the index once the store has exited, so the index is not stopped under a push and answers searches until the store is down. No correctness depends on that order. A store still running after 30 s (`SHADOW_STORE_STOP_S`) no longer holds the index back, and anything still running after 50 s (`SHADOW_STOP_GRACE_S`) is killed, inside Fly's 60 s kill timeout. The store answers SIGTERM by letting its owner finish the commit it is in and exiting 0; what its consumers had read and not committed is read again by the next process, from the cursors stored with the last commit. The exit came 6 to 7 s after the signal each of the three times it was watched (2026-11-11): the telemetry flush on that path did not return, and the 5 s deadline in `services/src/fatal.zig` ended the process with the line `fatal: telemetry flush did not finish within 5000 ms`. Why it does not return is not established. The exit status is zero when both stop cleanly, and otherwise the first non-zero status.

### What a failure looks like

| failure | search | what shows it |
|---|---|---|
| the store exits once, as its watchdog does on purpose | unaffected: the index serves on its last watermark and the new store process pushes the next | `[supervisor] store exited with status 1 after 4012s`, then `store stopped on its own, 1 time(s) without staying up 300s: starting it again in 1s, index keeps running`, then `started store as pid …` |
| the store cannot stay up | unaffected while each run pushes a watermark. If none arrives for 120 s the index reports not ready and answers 503 until one does | the same lines every 30 s at most, with the count rising; the `store_health` check on 8082, listed by `fly checks list` and counted in `fly status`, fails whenever the store is not listening; `sync_lag_s` in `/index/status` grows |
| the index exits | down until the new index process is ready; the store keeps following the relay and its pushes fail until then | `[supervisor] index exited with status …`, `index stopped on its own, …, store keeps running`; the `index_ready` check fails |
| the index cannot stay up | down | the same lines every 30 s at most; `index_ready` stays failing. The machine stays started: route away as in the [cutover runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/ops/cutover-to-store-slot.md#rollback) |

The count in the log line is the signal that a process is flapping: it goes back to 1 only after a run of 300 s. A machine restart is no longer one, since neither process exiting causes it.

`just test-supervise` runs the supervisor against stand-in processes for each row above and for the stop. The supervisor refuses a bash older than 5.2, which is what the image has. macOS ships 3.2, so run `brew install bash` there; the test looks on `PATH`, then in Homebrew's prefix, and `SUPERVISE_BASH` names one elsewhere. Homebrew's is 5.3, and the two differ where it matters here: under 5.2 `wait -n` loses a process that died by a signal while the shell was busy, and a bare `return` in a trap handler returns the interrupted command's status. The supervisor uses neither, and `just test-supervise-image` runs the same tests under the image's own bash, in Docker, to keep it so.

The store process is started at niceness 10 (`STORE_NICE`) and the index's merge threads run at niceness 19 (`INDEX_MERGE_NICE`), so when background work and a search want a CPU at the same time the search gets it. Niceness orders work inside the machine. It does nothing about a shared-CPU machine that has used up its quota; [sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/sizing.md#measured-on-the-machine-2026-11-11) has what that costs.

On a volume without a bootstrap receipt the supervisor starts nothing and waits, so an operator can run the bootstrap on the machine.

| process | listens on | reachable from |
|---|---|---|
| index, search | 8080 | the public `fly.dev` hostname, as on production slots |
| index, admin | 127.0.0.1:8081 | the store process only |
| store | 8082 | the machine's private address; no public service |

## Volume

One volume mounted at `/data`.

| path | holds |
|---|---|
| `/data/store` | the zkv directory store; the store process is its only writer |
| `/data/tantivy` | the serving index, in the layout the reader expects |
| `/data/tantivy-rebuild` | an index being rebuilt from the store, swapped in by rename |
| `/data/bootstrap` | scratch for the bundle during bootstrap, removed at the end |
| `/data/bootstrap.json` | the bootstrap receipt; the supervisor starts nothing without it |

The volume is 40 GB and the machine has four shared CPUs and 4 GB of memory. [Sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/sizing.md) has the arithmetic, the cost and what was not measured.

## Configuration

The supervisor sets the store process's role and paths itself: `MODE=store`, `STORE_DIR=/data/store` and `PORT` from `STORE_PORT`. It runs the two binaries from `/app`, or from `SHADOW_APP` when a test points it at stand-ins.

Set in the Fly configuration, not secret:

| name | purpose |
|---|---|
| `STORE_PORT` | port for the store process, 8082 |
| `INDEX_RETRIEVAL` | retrieval strategy; production pins `global-merge` in the index deployment |
| `INDEX_MAX_SYNC_LAG_S` | readiness limit on index lag, 120 as on production slots |
| `INDEX_PUSH_INTERVAL_MS` | read by the store process: its rest between pushes to the index while it keeps up, 5000. Each push is an index commit, so the index commits about 12 times a minute and a change reaches search at most about 5 s after the store has it; the production poller runs every 40 s |
| `INDEX_MERGE_MIN_SEGMENTS`, `INDEX_MERGE_LEVEL_LOG_SIZE`, `INDEX_MERGE_MIN_LAYER_SIZE`, `INDEX_MERGE_DELETE_RATIO`, `INDEX_MERGE_MAX_DOCS` | the index writer's [merge settings](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md#segment-merges): 3, 3, 1000, 0.2 and 1000000000. A size level merges at three segments or when a segment is more than a fifth deleted; levels are a factor of eight apart starting at 1,000 documents; no segment is too large to merge. Unset, the reader keeps Tantivy's defaults, which never remove deleted documents from an index that is not rebuilt |
| `INDEX_MERGE_NICE` | niceness of the index's merge threads, 19 |
| `INDEX_WARM_SEGMENT_FILES` | file extensions of each new index segment that the reader [reads into the page cache](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md#page-cache-warming) in the background: `term,fast,fieldnorm,idx`, 2.7 GB of the 4.7 GB index on 2026-11-11. The stored profiles (`store`, 1.9 GB) are left out because 4 GB of memory cannot hold them as well |
| `STORE_NICE` | niceness the supervisor starts the store process at, 10 when unset |
| the store process's bootstrap relay cursor | optional, set once for the first start; the name comes with the store process |

Secrets the app needs:

| name | where the value comes from |
|---|---|
| the store process's bearer secret for its read endpoints | new and random for this app; the name comes with the store process |
| `ADMIN_SECRET` | new and random for this app. It guards the index's loopback admin endpoints, which the store process will call. It must not be the Worker's secret of the same name |
| `INDEX_R2_ENDPOINT`, `INDEX_R2_BUCKET` | the same account endpoint and bucket the production slots and the index deployment use |
| `INDEX_R2_ACCESS_KEY_ID`, `INDEX_R2_SECRET_ACCESS_KEY` | a new R2 token with Object Read only on that bucket. The routing record lives in the same bucket, so the publisher's read-write token must not be reused. Needed only for bootstrap and can be unset afterwards |
| `LOGFIRE_WRITE_TOKEN` | optional; the write token of the Logfire `typeahead` project that the ingester uses today |

Secrets production has that the shadow must not: `TURSO_URL` and `TURSO_AUTH_TOKEN` (ingester and production slots) and `TYPEAHEAD_SECRET` (the ingester's copy of the Worker admin secret). `TYPEAHEAD_URL` goes with them. The supervisor exits if any of the three secrets is set.

## Bootstrap

The [bootstrap runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/bootstrap.md) covers how the volume gets its first index and store, the disk it needs, and what must hold before the store process advances a cursor.

## Not done yet

- The store process. Until its role lands in the binary, the store side of the image only exits.
- The store loader and cursor seeding in the bootstrap script. Seeding the index alone works.
- The index bridge, so the index is not updated after bootstrap and reports not ready once its watermark is older than the lag limit.
- Any Fly resource, and any measurement on a Fly volume.
- A run of the comparison job. The store process stays unreachable from outside the machine; the comparison's corpus walk [runs on the machine](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md#corpus-phase-on-the-shadow-machine) instead.

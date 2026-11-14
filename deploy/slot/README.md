# Store slot

Packaging for one slot of the search service: a single Fly machine with a single volume that runs the store process and the Tantivy index process side by side. Both slots, `typeahead-shadow-a` and `typeahead-shadow-b`, run this image. One serves and the other stands by; [operating](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operating.md) has the procedures and [how it works](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/how-it-works.md#inside-one-slot) the design. The app names, this directory and the `SHADOW_*` variables keep the name the slots had while they ran beside the system they replaced.

## What a slot holds

The store process follows the relay, the PLC directory and the labeler itself, keeps every account in a store on the volume, and feeds the index on the same machine. A slot holds no credential that can write anywhere else. Its one connection to the Worker is outbound and read-only: the store process pulls moderation policy and indexing requests from it ([the control database](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operating.md#the-control-database)). Its origin is in the transport's list of allowed origins in [`src/search-route.ts`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/search-route.ts), so the routing record can name it. Only [`slot_route.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/route/slot_route.py) writes that record, as [flip the route](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operating.md#flip-the-route) describes.

The store process is the binary built from `services/`, which the image installs as `typeahead-store`, the name it had when it also ran as the ingester. It runs as the store with `MODE=store` and as the store's loader with `MODE=store-bootstrap`, and refuses any other mode. The supervisor always starts it with `MODE=store`, and refuses to start at all when `TURSO_URL`, `TURSO_AUTH_TOKEN` or `TYPEAHEAD_SECRET` is set: those were the old system's write credentials, and a slot must not hold them.

## Build

From the repository root:

```sh
docker build --platform linux/amd64 -f deploy/slot/Dockerfile -t typeahead-slot:local .
```

The image holds the store binary twice. `typeahead-store` is built for any x86-64 CPU. `typeahead-store.aes` is the same source built with `-Dcpu=x86_64_v2+aes+avx+pclmul`: Zig's standard library chooses its AES and GHASH code when it compiles, and uses the CPU's instructions only when the target has those three features. The baseline build decrypts the relay's TLS stream (bsky.network picks `TLS_AES_256_GCM_SHA384`) in software, which measured as about half of the relay reader's CPU ([collector CPU, 2026-11-12](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/history/evals/collector-cpu-20261112/README.md)). A binary built for instructions the CPU lacks dies with SIGILL, so the supervisor reads the `flags` line of `/proc/cpuinfo` at start and runs the `.aes` build only when it lists every feature that build needs (`cx16 lahf_lm popcnt pni ssse3 sse4_1 sse4_2 avx aes pclmulqdq`); on any other machine it runs the baseline build. Its first lines say which: `store binary: the build that uses the CPU's AES`, or `store binary: the baseline build; this CPU does not report …`. Both machines the slots run on reported all of them on 2026-11-12 (an Intel Xeon under `typeahead-shadow-a`, an AMD EPYC under `typeahead-shadow-b`); that is two hosts, not a guarantee about Fly's fleet, which is why the choice is made on the machine. `bootstrap.sh` uses the baseline build.

The Zig compiler, the Rust toolchain and both base images are pinned by version and checksum. The Zig steps run with the network off from the vendored dependencies; Cargo fetches the locked crates in one step and compiles offline in the next.

## Processes

The supervisor starts both processes, prefixes each line of their output with `[store]` or `[index]`, and keeps both running. It looks at them once a second (`SLOT_TICK_S`). A process that exits on its own is started again in place and the other is left alone. The supervisor itself exits only when it is asked to stop, so a process that keeps failing never takes the machine down with it and never uses up Fly's restart limit.

The wait before a restart is 1 s the first time and doubles to a cap of 30 s (`SLOT_RESTART_MIN_S`, `SLOT_RESTART_MAX_S`). It returns to 1 s once the process has stayed up 300 s (`SLOT_RESTART_STABLE_S`). The cap is well under the 120 s the index serves without a watermark, so a store that runs long enough between exits to push one keeps search up.

Restarting one process alone is safe because the two share nothing but the index's loopback admin endpoints, and the [index bridge](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/how-it-works.md#inside-one-slot) keeps its progress in the index's own commit. A new store process reads the committed cursor and continues from it. A store process whose index went away fails its push, forgets what it knew, and reads the cursor again once the index answers. `just test-bridge` does both against a real index: it kills the bridge before or after a random request in each of sixty rounds, and the index just before and just after a commit.

A store process starts in the order `Stage` names in `services/src/store_process/role.zig`, because the index's watermark moves only when the bridge pushes. It opens the store for writing, reads the relay cursor, opens the bridge's read handle, then starts the owner, the bridge, and the relay, labeler, PLC and identity consumers. For 30 s after that it reads the store for nothing else: the owner leaves pruning, the label-expiry pass, change-log trimming, the handle repair and the health counts until then, `/store/*` answers 503 until its own handle is open, which starts when the 30 s are up and took 4 to 27 s more in the kills measured, and the first corpus count waits 300 s. `/health` answers throughout, with `store.taken_at` 0 and zero counts until the first refresh. The log has one line per stage (`start: bridge_started after 3301 ms`) and one for the first push the bridge commits. How long that leaves the index without a watermark, after a kill and after a machine start, is in [sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/slot/sizing.md#what-a-store-restart-costs-the-watermark).

SIGTERM and SIGINT stop the store first, with SIGTERM, and the index once the store has exited, so the index is not stopped under a push and answers searches until the store is down. No correctness depends on that order. A store still running after 30 s (`SLOT_STORE_STOP_S`) no longer holds the index back, and anything still running after 50 s (`SLOT_STOP_GRACE_S`) is killed, inside Fly's 60 s kill timeout. The store answers SIGTERM by letting its owner finish the commit it is in and exiting 0; what its consumers had read and not committed is read again by the next process, from the cursors stored with the last commit. The exit came 6 to 7 s after the signal each of the three times it was watched (2026-11-11): the telemetry flush on that path did not return, and the 5 s deadline in `services/src/fatal.zig` ended the process with the line `fatal: telemetry flush did not finish within 5000 ms`. Why it does not return is not established. The exit status is zero when both stop cleanly, and otherwise the first non-zero status.

### What a failure looks like

| failure | search | what shows it |
|---|---|---|
| the store exits once, as its watchdog does on purpose | unaffected: the index serves on its last watermark and the new store process pushes the next | `[supervisor] store exited with status 1 after 4012s`, then `store stopped on its own, 1 time(s) without staying up 300s: starting it again in 1s, index keeps running`, then `started store as pid …` |
| the store cannot stay up | unaffected while each run pushes a watermark. If none arrives for 120 s the index reports not ready and answers 503 until one does | the same lines every 30 s at most, with the count rising; the `store_health` check on 8082, listed by `fly checks list` and counted in `fly status`, fails whenever the store is not listening; `sync_lag_s` in `/index/status` grows |
| the index exits | down until the new index process is ready; the store keeps following the relay and its pushes fail until then | `[supervisor] index exited with status …`, `index stopped on its own, …, store keeps running`; the `index_ready` check fails |
| the index cannot stay up | down | the same lines every 30 s at most; `index_ready` stays failing. The machine stays started: route away as in the [store slot runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operating.md#roll-back) |

The count in the log line is the signal that a process is flapping: it goes back to 1 only after a run of 300 s. A machine restart is no longer one, since neither process exiting causes it.

`just test-supervise` runs the supervisor against stand-in processes for each row above and for the stop. The supervisor refuses a bash older than 5.2, which is what the image has. macOS ships 3.2, so run `brew install bash` there; the test looks on `PATH`, then in Homebrew's prefix, and `SUPERVISE_BASH` names one elsewhere. Homebrew's is 5.3, and the two differ where it matters here: under 5.2 `wait -n` loses a process that died by a signal while the shell was busy, and a bare `return` in a trap handler returns the interrupted command's status. The supervisor uses neither, and `just test-supervise-image` runs the same tests under the image's own bash, in Docker, to keep it so.

The store process is started at niceness 10 (`STORE_NICE`) and the index's merge threads run at niceness 19 (`INDEX_MERGE_NICE`), so when background work and a search want a CPU at the same time the search gets it. Niceness orders work inside the machine. It does nothing about a shared-CPU machine that has used up its quota; [sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/slot/sizing.md#measured-on-the-machine-2026-11-11) has what that costs.

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

The volume is 40 GB and the machine has four shared CPUs and 4 GB of memory. [Sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/slot/sizing.md) has the arithmetic, the cost and what was not measured.

## Configuration

The supervisor sets the store process's role and paths itself: `MODE=store`, `STORE_DIR=/data/store` and `PORT` from `STORE_PORT`. It runs the two binaries from `/app`, or from `SLOT_APP` when a test points it at stand-ins.

Set in the Fly configuration, not secret:

| name | purpose |
|---|---|
| `STORE_PORT` | port for the store process, 8082 |
| `INDEX_RETRIEVAL` | retrieval strategy; production pins `global-merge` in the index deployment |
| `INDEX_MAX_SYNC_LAG_S` | readiness limit on index lag, 120 as on production slots |
| `INDEX_PUSH_INTERVAL_MS` | read by the store process: its rest between pushes to the index while it keeps up, 5000. Each push is an index commit, so the index commits about 12 times a minute and a change reaches search at most about 5 s after the store has it; the production poller runs every 40 s |
| `INDEX_MERGE_MIN_SEGMENTS`, `INDEX_MERGE_LEVEL_LOG_SIZE`, `INDEX_MERGE_MIN_LAYER_SIZE`, `INDEX_MERGE_DELETE_RATIO`, `INDEX_MERGE_MAX_DOCS` | the index writer's [merge settings](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md#segment-merges): 3, 3, 1000, 0.2 and 1000000000. A size level merges at three segments or when a segment is more than a fifth deleted; levels are a factor of eight apart starting at 1,000 documents; no segment is too large to merge. Unset, the reader keeps Tantivy's defaults, which never remove deleted documents from an index that is not rebuilt |
| `INDEX_RESYNC_AFTER` | read by the store process: Unix seconds, unset in `fly.toml`. Passed with one deploy (`fly deploy -e INDEX_RESYNC_AFTER=$(date +%s)`) it makes the bridge rewrite every document in the index once, in a full resync round, unless the last completed round started at or after that time. For an image that changes what a document holds; see [Turso retirement](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/history/turso-retirement.md#rollout). Run it on the slot that is not serving: the admission refuses a slot with a round running |
| `INDEX_MERGE_NICE` | niceness of the index's merge threads, 19 |
| `INDEX_WARM_SEGMENT_FILES` | file extensions of each new index segment that the reader [reads into the page cache](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md#page-cache-warming) in the background: `term,fast,fieldnorm,idx`, 2.7 GB of the 4.7 GB index on 2026-11-11. The stored profiles (`store`, 1.9 GB) are left out because 4 GB of memory cannot hold them as well |
| `STORE_NICE` | niceness the supervisor starts the store process at, 10 when unset |
| `CONTROL_URL` | read by the store process: the Worker's origin, `https://typeahead.waow.tech`. With `SLOT_CONTROL_SECRET` it turns the control loop on. With the secret missing the loop stays off, the store says so in its log and in `/health` (`control.state`), and everything else runs |
| `CONTROL_POLL_SECONDS` | read by the store process: seconds between polls of the Worker, 5 to 3600, 30 when unset |
| `CONTROL_REQUESTS_PER_POLL`, `CONTROL_REQUEST_BACKLOG` | read by the store process: indexing requests taken in one poll at most (20 when unset), and the number of due identity queue entries at which none is taken (100 when unset), so requests cannot crowd out the relay's identity signals |
| `PROFILE_FILL` | read by the store process: `1` runs the one-time [profile fill](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/history/turso-retirement.md#profile-fill), which copies the descriptions and banners Turso holds into the actors of this store that have none, through the Worker and the control loop's secret. Unset in `fly.toml`; passed with one deploy (`fly deploy -e PROFILE_FILL=1`). A store that has finished the pass ignores it |
| `PROFILE_FILL_PAGE`, `PROFILE_FILL_STEP_SECONDS`, `PROFILE_FILL_LAG_LIMIT` | read by the store process: rows of Turso's table one page reads (500 when unset, at most 1000), seconds between pages (1 when unset), and the bridge lag in change-log entries above which no page is asked for (50000 when unset) |
| the store process's bootstrap relay cursor | optional, set once for the first start; the name comes with the store process |

Secrets the app needs:

| name | where the value comes from |
|---|---|
| the store process's bearer secret for its read endpoints | new and random for this app; the name comes with the store process |
| `ADMIN_SECRET` | new and random for this app. It guards the index's loopback admin endpoints, which the store process calls. It must not be the Worker's secret of the same name |
| `INDEX_R2_ENDPOINT`, `INDEX_R2_BUCKET` | the account endpoint and the `typeahead-index` bucket, which holds the bundles a bootstrap reads |
| `INDEX_R2_ACCESS_KEY_ID`, `INDEX_R2_SECRET_ACCESS_KEY` | a new R2 token with Object Read only on that bucket. The routing record lives in the same bucket, so a read-write token must not be used. Needed only for bootstrap and can be unset afterwards |
| `LOGFIRE_WRITE_TOKEN` | optional; the write token of the Logfire `typeahead` project |
| `SLOT_CONTROL_SECRET` | the bearer secret for the Worker's `/slot/control/*`, at least 32 bytes, the same value on the Worker and on every slot. It reads the control record and can change nothing. It must not be the Worker's admin secret, which those endpoints refuse. Not set anywhere as of 2026-11-12; set it with `fly secrets set --stage` so that setting it restarts nothing |

## Bootstrap

The [bootstrap runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/slot/bootstrap.md) covers how the volume gets its first index and store, the disk it needs, and what must hold before the store process advances a cursor.

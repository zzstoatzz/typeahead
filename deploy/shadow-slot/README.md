# Shadow slot

Packaging for one slot of the [embedded-store service](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md): a single Fly machine with a single volume that runs the store process and the Tantivy index process side by side. It is meant to run beside production with no route pointing at it, so its answers can be compared before it serves anyone.

Status, 2026-11-10: the Fly app `typeahead-shadow-a` exists with a 40 GB volume, and its index was bootstrapped on the machine from bundle `b1794333857-cp-e3e8808936` (13,164,304 actors). The store process is not running there yet, so the index is unfed and answers searches with 503: its moderation policy has to be refreshed every 300 seconds, which the store process will do. No route points at the app. The supervisor was also exercised locally; see [validation](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/validation.md).

## How it differs from a production slot

A production slot (`typeahead-tantivy-a` and `-b`) runs the Tantivy reader plus a poller that reads Turso, is installed from a published bundle by the refresh controller twice a day, and is sized at two shared CPUs, 1 GB of memory and a 20 GB volume. The ingester is a separate app.

The shadow slot puts the ingester's successor on the same machine as the reader. It holds no Turso credentials and no Worker admin secret, takes a bundle once at bootstrap, and afterwards is fed only by its own store. Its origin is in the transport's list of allowed origins in [`src/search-route.ts`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/search-route.ts), so the routing record can name it once a transport built from that list is deployed. The refresh controller never writes such a record; only [`store_slot_route.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/snapshot_refresh/store_slot_route.py) does, as the [cutover runbook](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/ops/cutover-to-store-slot.md) describes.

The store process is a role of the ingester binary, selected with `MODE=store`, and is still being built. The binary in an image built from a tree without that role refuses the mode and exits, and the supervisor exits with it. The supervisor always starts the binary in that mode and refuses to start at all when Turso or Worker credentials are present, so this image cannot become a second production ingester by mistake.

## Build

From the repository root:

```sh
docker build --platform linux/amd64 -f deploy/shadow-slot/Dockerfile -t typeahead-shadow-slot:local .
```

The Zig compiler, the Rust toolchain and both base images are pinned by version and checksum. The Zig steps run with the network off from the vendored dependencies; Cargo fetches the locked crates in one step and compiles offline in the next. The reader built here is not byte-identical to the released production reader, so its `binary_sha256` differs.

## Processes

The supervisor starts both processes, prefixes each line of their output with `[store]` or `[index]`, and forwards SIGTERM and SIGINT. If either process exits on its own, it stops the other and exits non-zero, and Fly restarts the machine. Neither process is restarted alone: the store process commits its cursors with its data, so a whole-machine restart is always safe, and a half-running slot would look healthy to one of its two checks. A requested stop exits zero when both processes stop cleanly.

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

The supervisor sets the store process's role and paths itself: `MODE=store`, `STORE_DIR=/data/store` and `PORT` from `STORE_PORT`.

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

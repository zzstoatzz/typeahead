# typeahead index design

bounded-work prefix serving + authority ranking for the atproto actor
typeahead service. consolidates two independent tracks. neither blocks
the other.

## why this exists

the current serving path (SQLite FTS5 + LIKE prefix + batched IN hydration)
does work proportional to corpus size on every request. a single weird
query — `q=éric`, `q=natemoo.re`, `q=jay.bsky.team` — produces a sqlite
plan that walks postings or scans cold pages for seconds, occupies a pool
slot for that duration, and cascades. no amount of pool tuning, overfetch
trimming, or wait-budget tightening bounds the per-request CPU/I/O cost.
the architecture is wrong for the goal.

what's right: **no request path may do work proportional to corpus size.**
every read in `/search?q=…` must be a bounded number of B-tree point
lookups against an index whose access cost is set by what was asked for,
not by what the corpus contains.

ground-truth check from research:

- bsky's at-scale answer is outsource to opensearch with a dedicated
  `search_as_you_type` field, pagerank precomputed every 24h via batch.
  the open-source typeahead query is BM25 only; pagerank is a stored
  field that's not consulted (whatever does consult it is closed source).
- no independent atproto AppView has shipped prefix actor search.
  constellation, slingshot, hubble all serve exact-key lookups.
  plyr.fm explicitly delegates typeahead to us.
- for our constraints (256MB Fly VM, 5.9M actors, 10GB volume), the
  SQLite-as-KV approach wins on operational simplicity over fst/MARISA
  because the top-N-by-score problem is solved at *build time*, not at
  query time — we never ask SQLite to rank.

## tracks

```
TRACK 1: bounded serving (this week)
  goal: serve /search with O(1)-in-corpus work
  shape: precomputed prefix→top-N index + live overlay, all zig
  ranking: existing quality_score (sparse but fine)
  ships independently

TRACK 2: ranking quality (parallel, lands later)
  goal: every actor has a real authority signal
  shape: follow graph + periodic pagerank compute
  output: writes into actors.quality_score
  track 1 picks it up automatically on next build
```

track 1 must not depend on track 2. track 2 improves the score function
that track 1 already consumes. they merge naturally at `quality_score`.

---

# track 1: bounded serving

## components

```
typeahead-search (existing fly app; one image; two process groups)

  process group: app
    machines:  N≥1, each with its own fly volume (typeahead_search_data)
    serves:    /search via prefix_top_base ∪ prefix_overlay
    sync:      turso → local actors sync (existing)
    overlay:   new module in sync loop; diffs prefixes on actor change,
               writes prefix_overlay + actor_overlay
    manifest watcher: polls indexer.process.typeahead-search.internal/manifest
                      on a tick; downloads new snapshot to local volume on change

  process group: indexer
    machine:   1 (auto_start = false; manual or cron-triggered start)
    volume:    typeahead_indexer_data (own volume; not shared with app)
    builds:    /data/build-<id>/index.db off-path, full rebuild
    serves:    HTTP on fly's internal 6PN network:
               GET /manifest.json       → current manifest
               GET /snapshot/<id>.db    → snapshot file stream
               GET /snapshot/<id>.sha256 → checksum
    cadence:   built-in loop, sleeps between builds; or one-shot triggered
               externally
    isolation: separate volume + machine; builder OOM/crash cannot kill
               serving

shared zig modules (used by both process groups via the same binary):
  normalize.zig   query/handle/display normalization
  prefixes.zig    prefix derivation from normalized strings
  scoring.zig     score function (reads quality_score; pluggable)
  pack.zig        binary blob encoding for actor_compact and prefix_top
```

**fly volume constraint**: a fly volume attaches to exactly one machine
at a time. the indexer and app processes cannot share a volume. they
share a binary image instead. snapshot files transfer over fly's internal
network (6PN, free, low latency, in-region) from the indexer's volume
to each app machine's volume.

`fly.toml` shape:

```toml
app = 'typeahead-search'

[build]
  dockerfile = 'Dockerfile'

[processes]
  app     = './typeahead-ingester'                  # serving (existing)
  indexer = './typeahead-ingester --mode=indexer'   # new

[[mounts]]
  source = 'typeahead_search_data'
  destination = '/data'
  processes = ['app']

[[mounts]]
  source = 'typeahead_indexer_data'
  destination = '/data'
  processes = ['indexer']

[[vm]]
  size = 'performance-2x'    # 2 cpu / 4 GB
  processes = ['indexer']

[[vm]]
  size = 'shared-cpu-1x'     # 256 MB (existing)
  processes = ['app']
```

deferred v2 option: if/when we add multi-region read replicas, swap the
in-network snapshot fetch for object storage (R2 or S3) so the indexer
machine can be stopped between builds and replicas can fetch from a
durable source. v1 stays in-network — simpler, cheaper, one fewer
credential to manage.

## data model

served from App B's local sqlite, three logical layers:

```sql
-- BASE SNAPSHOT (immutable per build, atomic swap on rebuild)

CREATE TABLE prefix_top_base (
  key   TEXT PRIMARY KEY,        -- 'h:nate', 'n:sarah', 'e:nate.fm'
  dids  BLOB NOT NULL            -- packed [score f32, did_bin 16b]*
) WITHOUT ROWID;

CREATE TABLE actor_compact_base (
  did   TEXT PRIMARY KEY,        -- DID text; binary form is v2
  blob  BLOB NOT NULL            -- packed {handle, display, avatar,
                                 --   labels, pds, created_at, associated,
                                 --   hidden, quality_score}
) WITHOUT ROWID;

CREATE TABLE index_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- mandatory keys:
--   build_id            ulid-ish
--   built_at            unix seconds
--   source_watermark    turso updated_at when build started
--   builder_version     git sha
--   top_n_short         300
--   top_n_medium        200
--   top_n_long          100
--   overlay_top_n       100
--   hydrate_cap         50
--   normalize_version   tag string for normalize.zig; bump when
--                       semantics change so indexer + serving stay aligned

-- LIVE OVERLAY (mutable; small; compacted into next base on rebuild)

CREATE TABLE actor_overlay (
  did         TEXT PRIMARY KEY,
  blob        BLOB,              -- NULL = tombstone (delete)
  hidden      INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE prefix_overlay (
  key         TEXT NOT NULL,
  did         TEXT NOT NULL,
  score       REAL NOT NULL,
  op          INTEGER NOT NULL,  -- 1=add, 0=suppress base entry
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (key, did)
) WITHOUT ROWID;
CREATE INDEX idx_overlay_updated_at ON prefix_overlay(updated_at);
```

design notes:

- key namespace stays a `TEXT` prefix (`h:` handle prefix, `n:` display-name
  token prefix, `e:` exact handle). one `WHERE key IN (?,?,?,…)` query
  covers all three. equivalent to splitting into (ns, text) columns,
  more readable.
- `actor_overlay.blob NULL` is a tombstone. serving merge skips the DID
  entirely, even if base has it.
- `prefix_overlay.op = 0` is an explicit suppression. used when an actor
  renames: old prefixes get `op=0` rows so the actor stops appearing under
  them until next compaction. without this we'd surface stale handles
  for hours.
- `WITHOUT ROWID` makes both tables clustered B-trees on the natural key.
  halves lookup hops vs the default schema.

## the builder

a zig binary that runs once per cadence. writes off-path. atomic swap.

```
INPUTS:
  TURSO_URL, TURSO_AUTH_TOKEN
  /data/index/                  (output root)
  config (TOP_N_*, prefix max len, hydrate cap)

OUTPUTS:
  /data/index/build-<id>/index.db    (sqlite with prefix_top_base + actor_compact_base + index_meta)
  /data/index/manifest.json          (rewritten atomically at end)
```

algorithm:

```
1. begin: pick build_id = ulid(); mkdir /data/index/build-<id>/
2. stream actors from turso:
   SELECT did, handle, display_name, avatar_url, labels, pds, created_at,
          associated, hidden, quality_score
   FROM actors
   WHERE handle != ''
   ORDER BY did   -- stable iteration, restartable on crash
3. for each row:
   a. write actor_compact_base row (pack blob, INSERT)
   b. compute prefix emissions via shared prefixes.zig:
        handle_norm = normalize(handle)
        name_tokens = tokenize_display(display_name)
        emit ('h:' + handle_norm[0..i], did, score) for i in 1..min(len, PREFIX_MAX)
        emit ('n:' + tok[0..i], did, score) for each tok, each i
        emit ('e:' + handle_norm, did, score)
   c. write emissions to a temporary unkeyed table (ordered by key, score desc)
4. group emissions:
   SELECT key, did, score FROM emissions ORDER BY key, score DESC;
   for each key, take the top-N where N depends on len(key):
     len 1-3 → TOP_N_SHORT (300)
     len 4-6 → TOP_N_MEDIUM (200)
     len 7+  → TOP_N_LONG (100)
5. pack top-N into BLOB ([f32 score, 16-byte did_bin]*) and INSERT into prefix_top_base
6. write index_meta rows (build_id, built_at, source_watermark, top_n_*, etc.)
7. ANALYZE, VACUUM
8. write manifest.json atomically:
   {
     "active_build": "<id>",
     "previous_build": "<old_id_or_null>",
     "promoted_at": <unix>,
     "source_watermark": <unix>,
     "validation": {"actor_count": N, "prefix_count": P, "byte_size": B}
   }
9. App B reads manifest on a tick (every 30s) and swaps active snapshot
   on change. switch is done by ATTACH DATABASE under a new alias and
   re-binding the prepared statements; no in-place mutation of the old.
10. old snapshot stays on disk for one more cycle as rollback. delete on
    the build after that.
```

sizing (5.9M actors, target):

```
prefix emissions:    ~150M tuples before group/cap
unique prefix keys:  ~1-5M
avg dids per key:    150 (skewed; short prefixes hit the cap)
prefix_top_base:     ~3-8 GB
actor_compact_base:  ~1.2 GB
total per snapshot:  ~4-10 GB
volume needs:        20 GB Fly volume (currently 10; extend before going live)
build time:          10-30 min on shared-cpu Fly machine
```

## the overlay updater

lives inside App B's existing sync loop (`ingester/src/db/sync.zig`).
runs on every actor row applied locally. shared code with builder via
the prefixes.zig module.

```
on each (new_actor) applied from turso:
  old_compact = SELECT blob FROM actor_compact_base WHERE did = ?
                ∪ SELECT blob FROM actor_overlay WHERE did = ?
                (overlay wins if present)

  if new_actor.deleted:                              # tombstone
    UPSERT actor_overlay (did, blob=NULL, hidden=0, updated_at=now)
    for key in derive_prefixes(decode(old_compact)):
      UPSERT prefix_overlay (key, did, score=0, op=0, updated_at=now)
    return

  new_prefixes = derive_prefixes(new_actor)
  old_prefixes = derive_prefixes(decode(old_compact)) if old_compact else ∅

  added       = new_prefixes - old_prefixes
  removed     = old_prefixes - new_prefixes
  score_chg   = (old_compact and new_actor.score != decode(old_compact).score)
                and (new_prefixes & old_prefixes)

  UPSERT actor_overlay (did, blob=pack(new_actor), hidden=new_actor.hidden,
                        updated_at=now)
  for key in added:     UPSERT prefix_overlay (..., op=1)
  for key in removed:   UPSERT prefix_overlay (..., score=0, op=0)
  for key in score_chg: UPSERT prefix_overlay (..., op=1, score=new_score)
```

invariants:

- overlay is bounded between rebuilds. typical rebuild cadence 4-6h,
  typical handle/display change rate is low — overlay stays small (single-
  digit MB at most).
- compaction (next build) starts from current actors table state + last
  applied turso watermark. it does not need to "consume" overlay rows —
  the new base already reflects everything, so the indexer just truncates
  the overlay tables at swap time (or atomically swaps to an empty
  overlay alongside the new base).

## serving path

### multi-token semantics

single-token queries: union of `h:` + `e:` + `n:<query>` candidate sets,
dedupe by DID, rank by score.

multi-token queries (`"fig aka"`, `"john doe"`): the merge must take the
**intersection** of per-token `n:` candidate sets — an actor must appear
under every token's lookup. otherwise `n:aka` returns spammy "aka"
accounts that pollute the `fig aka` result. final score is the sum (or
min — choose at impl time, both work) of per-token scores within the
intersection.

this is `bool_prefix` with `operator: and` in OpenSearch / Palomar
terms. we do NOT do phrase queries, word-order constraints, or BM25;
just "has-all-tokens" + authority ranking.

```
queryKeys("fig aka")  →  ["n:fig", "n:aka"]   (handle key skipped: query has whitespace)
lookup each, get:
  n:fig    → [DID_A, DID_B, DID_C, DID_D, …]   (top by score)
  n:aka    → [DID_B, DID_C, DID_E, …]

candidates: DID_B, DID_C     (appear under BOTH)
score: sum of per-token scores
hydrate top HYDRATE_CAP, return top limit
```

### code shape

```zig
pub fn search(arena: Allocator, q: []const u8, limit: usize) ![]Actor {
    const norm = normalize.query(arena, q);
    if (norm.len == 0) return &.{};

    const keys = try prefixes.queryKeys(arena, norm);
    const tokens = try prefixes.displayTokens(arena, norm);

    // 2 SELECTs total via WHERE key IN (?,?,…). atomic snapshot read.
    const base_hits    = try snapshot.queryPrefixBase(arena, keys);
    const overlay_hits = try snapshot.queryPrefixOverlay(arena, keys);

    // merge:
    //  - 1 token: union candidate sets, dedupe by DID, rank by score
    //  - N tokens: intersect per-token n: candidate sets (AND semantics),
    //    rank by sum of per-token scores
    //  - overlay op=0 suppresses base entry
    //  - overlay op=1 replaces base score
    //  - cap at HYDRATE_CAP after sort
    const dids = try merge.combine(arena, base_hits, overlay_hits, .{
        .tokens = tokens,
        .cap = config.hydrate_cap,
    });

    // hydrate. overlay first (newer blob), base for the rest. 1-2 SELECTs.
    const overlay_blobs = try snapshot.queryActorOverlay(arena, dids);
    const base_blobs    = try snapshot.queryActorBase(arena, dids - overlay_blobs.keys());

    return assemble(arena, dids, overlay_blobs, base_blobs, .{
        .skip_hidden = true, .limit = limit,
    });
}
```

worst case: 4 sqlite point/range lookups. with `WITHOUT ROWID` clustered
B-trees and ≤50 DIDs per lookup, ≤200 page reads total. mmap-warm sub-ms;
mmap-cold ≤20ms. **independent of corpus size.**

## snapshot lifecycle

```
indexer machine volume (typeahead_indexer_data):
  /data/
    manifest.json                   served at /manifest.json (authoritative)
    build-01J2X.../index.db         active build (largest, served on demand)
    build-01J2W.../index.db         previous build (kept one cycle)

app machine volume (typeahead_search_data, one per app machine):
  /data/
    local.db                        existing — actors, overlay tables, sync state
    index/
      manifest.json                 last-fetched copy of indexer's manifest
      build-01J2X.../index.db       active local copy
      build-01J2W.../index.db       previous local copy (rollback)
```

each app machine maintains its own LOCAL copy of the active snapshot
under `/data/index/`. App opens the active snapshot via
`ATTACH DATABASE` under a fixed alias (`idx`). all prefix/hydrate queries
use `idx.prefix_top_base` etc. overlay tables live in `local.db`
(mutable, written by the sync loop, not part of the immutable snapshot).

snapshot promotion (full flow):

```
1. indexer finishes writing /data/build-<new>/index.db + .sha256
2. indexer writes /data/manifest.json.tmp with new active_build + checksums
3. atomic rename manifest.json.tmp → manifest.json
4. each app machine's manifest watcher polls (every 30s) at:
     http://<machine-id>.vm.typeahead-search.internal:8081/manifest.json
   (the indexer process serves manifest+snapshot on internal port 8081
   over fly's 6PN network — no public exposure)
5. on change, app machine streams the new snapshot:
     GET /snapshot/<new>.db → write to /data/index/build-<new>/index.db.tmp
     GET /snapshot/<new>.sha256 → verify
     atomic rename index.db.tmp → index.db
6. App begins ATTACH on the new local snapshot
7. App re-prepares queries against the new alias
8. App atomically swaps the active alias under a brief write lock
9. App DETACHes the old snapshot
10. old local snapshot directory deleted after one more promotion cycle
```

rollback: if the new snapshot is bad (validation fails, queries return
empty, sentinel queries report stale or wrong results), rewrite
indexer's manifest.json to point at `previous_build`. each app machine
picks it up on the next tick and re-attaches the old local copy (still
on disk). **the active snapshot file is never mutated in place** —
rollback is just a manifest write + DETACH/ATTACH cycle.

failure modes:
- **indexer machine unreachable**: app machines keep serving from their
  current local snapshot. staleness grows but service stays up. when
  indexer comes back, normal poll picks up new manifests.
- **indexer machine OOM mid-build**: build-<new>/ files are partial,
  not promoted (manifest.json still points at previous). next indexer
  run sweeps unreferenced build-<id>/ dirs.
- **app machine disk fills during snapshot download**: download fails,
  watcher retries next tick. monitor `/data` usage on app machines;
  current overhead ~10-20GB total.
- **sha256 mismatch on download**: discard, retry. log + page if it
  keeps failing.

## migration

1. land builder binary + indexer mode behind a build flag. new tables
   are local-only to App B and the indexer machine — created
   idempotently by a Zig `index/schema.zig` module on first open. NOT
   a Zug migration (Zug is for Turso shared schema; these tables don't
   exist in Turso). App B's existing FTS path untouched.
2. run indexer once manually. validate sizes, query a few prefixes from
   the resulting snapshot via sqlite3 directly.
3. wire overlay updater into App B's sync loop behind env flag
   `INDEX_OVERLAY=1`. defaults OFF.
4. add `USE_PREFIX_INDEX=1` env flag to App B's search handler. when set,
   route to new path. defaults OFF.
5. turn `INDEX_OVERLAY=1` on in App B. let it run for an hour. confirm
   overlay row counts stay bounded.
6. smoke test via Option A (direct App B URL, public unaffected).
7. flip `USE_PREFIX_INDEX=1` in production with backstop still on. one
   week bake.
8. drop `EMERGENCY_PROXY_BSKY`. baked.
9. remove actors_fts + the FTS code path in a later cleanup.

each step reversible by flipping a flag, no schema migrations to undo.

---

# track 2: follow graph + pagerank

independent of track 1. produces a real authority score that overwrites
`actors.quality_score`. track 1's next build picks it up automatically.

## data model (start small; measure before scaling)

```sql
-- ingester-local (NOT turso; turso row counts get expensive at follow scale)
CREATE TABLE follows (
  subject TEXT NOT NULL,    -- the follower (subject of the follow record)
  object  TEXT NOT NULL,    -- the followed
  created_at INTEGER NOT NULL,
  PRIMARY KEY (subject, object)
) WITHOUT ROWID;

CREATE INDEX idx_follows_object ON follows(object);
```

storage budget: do not commit to a size until measured. plan in phases:

```
PHASE 0  (sample):    follows for top 10K indexed actors. measure node/edge counts.
PHASE 1  (broaden):   follows where target is any indexed actor (5.9M).
PHASE 2  (full):      follows where source or target is any indexed actor.

stop and remeasure between phases. do not start by crawling the full
network. bsky's full follow graph is multi-TB; we're not there.
```

## backfill

continuous updates land via jetstream (worker already receives the events
— just needs to forward them to ingester via existing /admin/ingest path,
ingester writes to local `follows` table).

history is missing for follows that pre-date our jetstream subscription.
get it via `com.atproto.repo.listRecords` on each indexed actor:

```
for each indexed actor (paginated by quality_score desc, or stream order):
  for each repo cursor:
    fetch app.bsky.graph.follow records from the actor's PDS via slingshot
    upsert into follows
  rate limit: 5 req/s to each PDS, ~50 req/s aggregate
  ETA: governed by # indexed actors × avg follow record count
       phase 0 is hours; phase 1 is days; full is weeks
```

backfill is a separate zig binary or `--mode=follow-backfill`. resumable
via a cursor table.

## pagerank compute

dedicated fly machine (or one-shot job machine, `performance-2x` or
larger for RAM). zig binary, `--mode=pagerank`.

algorithm: standard PageRank with damping d=0.85, until convergence
(L1 delta < 1e-6 or 50 iterations whichever first). CSR (compressed
sparse row) layout for the adjacency matrix:

```
1. load follows into memory:
   - node_id_of[did] = u32 index
   - csr_indptr[N+1] = u32 offsets into csr_indices
   - csr_indices[E] = u32 target ids
   - read sequentially from follows table sorted by (subject, object)
2. allocate rank[N], rank_new[N] as f32
3. iterate:
   for each src in 0..N:
     out_deg = csr_indptr[src+1] - csr_indptr[src]
     if out_deg == 0: continue (dangling — distribute to all)
     contribution = rank[src] / out_deg
     for j in csr_indptr[src]..csr_indptr[src+1]:
       rank_new[csr_indices[j]] += contribution
   apply damping; check convergence; swap.
4. output: write (did, pagerank) → turso actors.quality_score via
   batched UPDATE.
```

memory at our scale:

```
nodes      ~6M     → node_id table ~150MB
edges      ~500M (phase 1)  → csr arrays ~3-4GB
rank, rank_new      → ~50MB
runtime estimate    → 30 iterations × ~10s/iter = 5 minutes
```

doesn't fit on a 256MB Fly machine. needs at minimum 4GB RAM. that's a
$25/mo `performance-2x` or a one-shot scheduled machine that costs
pennies per run.

cadence: every 6-24h. matches bsky's published 24h cadence.

## integration with track 1

```
pagerank job writes actors.quality_score in turso
→ App B's sync replicates actors row updates
→ overlay updater detects score_changed prefixes → writes prefix_overlay
  with op=1 and the new score
→ next indexer rebuild (4-6h later) folds the new scores into prefix_top_base
```

no coordination needed. track 1 already plumbs `quality_score` through
the index. swap the score function under it and the index updates
automatically.

## quality bar

we should not flip to pagerank-as-quality_score until it's measurably
better than the current `log10(followers) + ...` heuristic. validation:

- bench against the canonical query corpus (already in scripts/bench.py).
  pagerank should improve parity with bsky's top results on `aoc`, `phil`,
  `nate`, `alex`, `bob`, `car` — the queries we currently fail.
- spot-check ~50 hand-picked queries by domain knowledge.
- check the long tail: does some-random-low-follower account suddenly
  rank #1? pagerank can have surprising winners — usually fine but worth
  reviewing.

---

# known limitations

documented up front so they don't surprise anyone later. tracked here,
not in code comments alone, so they're visible to anyone reading the
architecture without crawling source.

1. **non-Latin scripts are unsearchable in v1.** `normalize.zig` drops
   every codepoint outside ASCII + Latin-1 Supplement + Latin
   Extended-A. A Japanese display name `佐藤` normalizes to the empty
   string and emits no `n:` prefix keys. The actor is still indexed by
   its (typically ASCII) handle. Mitigation: bump to full Unicode case
   folding (e.g., via a generated table from CLDR) when atproto adoption
   warrants. Until then, this is acceptable for the dominant search
   shape (typing a handle prefix).

2. **score function is sparse.** `quality_score` is populated for
   ~250 actors today (canonical-query backfill). Until track 2
   (pagerank) ships, ranking depends on a heuristic over followers/posts
   that's zero for the long tail. Track 1 ships independently with this
   limitation — the index is correct, just under-discriminating in the
   tail.

3. **handles with `_` lose the underscore.** atproto handles are
   spec'd as `[a-z0-9.-]` so this only matters for malformed handles;
   we strip `_` in `normalize.handle()` and index whatever's left. Users
   typing `_` in a query still see results, because `normalize.query()`
   keeps `_` and we look up multiple keys.

4. **base snapshot is hours stale at the long tail.** the overlay
   absorbs recent actor changes for prompt visibility, but score
   recomputation (especially track-2 pagerank) lands in the base
   snapshot at the next rebuild. expected lag: 4-6 hours for score
   ranking; minutes for actor existence/handle.

5. **single-region single-replica.** the snapshot transport (indexer
   process → app machine over fly internal 6PN) assumes one app machine.
   for multi-region replicas we'll move snapshots to object storage
   (R2 or S3). designed-for, not built-for.

---

# shared modules (used by both tracks)

```
ingester/src/index/normalize.zig
  pub fn query(arena, raw) []const u8           // for search input
  pub fn handle(arena, raw) []const u8          // for actor handles
  pub fn display(arena, raw) []const u8         // for display names
  pub fn tokens(arena, normalized) [][]const u8 // split for n: keys
  -- semantics:
  --   lowercase ASCII + common Latin folding (initial scope)
  --   strip combining marks (NFKD-ish; pure-zig table)
  --   drop emoji
  --   collapse internal whitespace
  --   token split: whitespace + ascii punct except dots in handles
  --   normalize_version constant: bump when behavior changes

ingester/src/index/prefixes.zig
  pub fn derive(arena, actor) []PrefixKey
  pub const PREFIX_MAX_LEN: usize = 16

ingester/src/index/scoring.zig
  pub fn score_of(actor) f32
  -- v1: returns actor.quality_score directly
  -- v2 (later): may incorporate recency, mod signals, etc.

ingester/src/index/pack.zig
  pub fn pack_actor(arena, actor) []u8       // for actor_compact blob
  pub fn unpack_actor(blob) Actor
  pub fn pack_did_list(arena, entries) []u8  // for prefix_top blob
  pub fn unpack_did_list(blob) DidListIter
```

normalize.zig is the most load-bearing — divergence between builder and
serving means the index serves stale/wrong results invisibly. one
canonical function. `normalize_version` constant is written into
`index_meta`; serving validates on snapshot load and refuses to use a
snapshot built with a different normalize_version.

---

# open questions remaining

1. **follow event forwarding from worker to ingester.** worker currently
   receives follow events but drops them on the ingester forwarding
   path. need to extend `/admin/ingest` to accept follow records.
   not blocking track 1.
2. **manifest watcher tick interval.** 30s feels right. tunable via env.
3. **rollback policy.** keep N=1 previous snapshot or N=2? cost is
   another 4-10GB volume. start with N=1.
4. **handle of indexer failure mid-build.** zig binary crashes; partial
   files in build-<id>/. cleanup logic: if manifest.json doesn't point
   at it, indexer's next run deletes any unreferenced build-<id>/ dirs.

---

# what does NOT change

the work from the current hardening iteration still serves us in the
new architecture:

- read-conn pool with lease+release
- closeReadConn lock-then-close race fix
- READ_POOL_SIZE / READ_POOL_WAIT_MS env-tunable
- dedicated meta_conn for /health
- WAL TRUNCATE per incremental sync
- per-phase timing logs

every one of those is still right under the new path — the new path
just makes per-query cost so small that pool exhaustion becomes
essentially impossible in normal operation. when it does happen, the
fail-fast 503 + worker turso fallback still applies.

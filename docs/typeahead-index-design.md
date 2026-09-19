# typeahead index design

Status: **historical design record**. This captures the rearchitecture plan and
many decisions from the FTS-to-prefix-index migration. For the current
operational architecture, start with `docs/architecture.md`,
`docs/operations.md`, and `CLAUDE.md`.

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
- for our constraints (256MB Fly VM, 5.9M actors, 20-30GB volumes), the
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

served from search service's local sqlite, three logical layers:

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

algorithm (as implemented in `services/src/index/builder.zig`):

```
1. begin: build_id = "b<unix_s>-<hex4>"; mkdir <build_root>/build-<id>/
   open index.db; pragmas (journal_mode=DELETE, synchronous=OFF,
   temp_store=FILE, cache_size=-200000); SQLITE_TMPDIR/TMPDIR := build_dir
   (so the file-backed sort spills onto the volume, not RAM/root fs).
2. ATTACH a SEPARATE scratch DB:  ATTACH '<build_dir>/staging.db' AS stage
   (stage.journal_mode=OFF, stage.synchronous=OFF — throwaway).
   CREATE TABLE stage.emissions(key, did, score).
3. acquire source rows. PRIMARY = export HTTP (TursoClient.exportToFile):
     GET /info → current_generation; GET /export/{gen} streamed to
     <build_dir>/source.db.part → rename source.db; ATTACH 'source.db' AS src
     (plain-path, SQL-quoted); validate (quick_check + count).
   FALLBACK = hrana v2 keyset pagination (INDEX_SOURCE=hrana_v2).
   DEV = INDEX_REUSE_SOURCE=<path> builds from an existing local export.
4. BEGIN; iterate  SELECT ... FROM src.actors WHERE handle != '' ORDER BY did:
   a. write actor_compact_base row (pack blob, INSERT) → index.db
   b. derive prefix emissions via shared prefixes.zig (deriveAllUnique):
        'h:' + handle_norm[0..i] for i in 1..min(len, PREFIX_MAX)
        'n:' + tok[0..i] per display token; 'e:' + handle_norm
   c. INSERT each (key, did, score) → stage.emissions
   COMMIT. (src detaches on return; source.db deleted now, before the
   disk-heavy index build, to lower peak volume usage.)
5. CREATE INDEX stage.idx_emissions_key_score ON emissions(key, score DESC)
   — file-backed sort, spills to SQLITE_TMPDIR (the volume), flat RAM.
6. group:  SELECT key, did, score FROM stage.emissions ORDER BY key, score DESC
   per key, take top-N by len(key): 1-3 → 300, 4-6 → 200, 7+ → 100.
7. pack top-N into BLOB ([f32 score, 16-byte did_bin]*) → prefix_top_base (index.db)
8. DETACH stage; DELETE staging.db  (frees the ~11GB emissions+index instantly,
   no VACUUM — index.db only ever held the real artifact, so it ships at size).
9. write index_meta; ANALYZE.  (no VACUUM step exists or is needed.)
```

steps 1–9 are implemented and validated. manifest write + atomic promotion
+ search service fetch/swap are the NEXT piece (see `## manifest contract` /
`## promotion gate` below) and are intentionally NOT wired yet — the first
Fly run is measurement-only (prove the builder produces a correct full-scale
artifact within RAM/disk budget), no promotion, no serving change, backstop
stays on.

### sizing (measured from real sample builds)

100k-actor sample produced (measured, not estimated):

```
emissions per actor:    24.5  (linear)
emissions total:        2.45M
distinct prefix keys:   1.6M
prefix_top_base bytes:  87 MB
actor_compact_base:     24 MB  (240 B/actor)
on-disk total:          350 MB
build time:             ~140 s
```

Extrapolation to 5.9M actors:

```
actor_compact_base:     ~1.4 GB   (linear in actor count)
prefix_top_base:
  short tier (cap=300):   ~18 MB  (saturates by 100k — constant from here)
  medium tier (cap=200):  ~250 MB (uncapped now, will bind at 5.9M)
  long tier (cap=100):    ~3 GB   (almost all uncapped; ~1-2 DIDs/key)
  e: keys:                ~242 MB (1 per actor, fixed 41 B)
total prefix_top_base:    ~3.5 GB
SQLite overhead:          +25-30%
TOTAL SNAPSHOT:           ~6-7 GB
```

### source acquisition: export HTTP, not row streaming

The original plan streamed rows over hrana and paginated to bound memory.
That's gone. The builder now pulls the corpus via the Turso **export
endpoints** — a binary SQLite page stream, ~60× faster than per-row JSON:

```
export endpoints (GET /info + /export/{gen}):  234 s  (3.2 GB, 13.6 MB/s)
hrana v3 /v3/cursor NDJSON (abandoned):       ~4 h    (~200 KB/s)
```

The export body streams straight to `source.db` via a `std.Io.File.Writer`
(flat memory, no buffering), then we `ATTACH` it and iterate. Memory during
fetch is constant regardless of corpus size. hrana v2 pagination survives
only as an `INDEX_SOURCE=hrana_v2` fallback. (See `docs/turso-research.md`
and `../notes/databases/turso.md` for the wire format.)

### build-time + memory (measured at full scale)

Authoritative full-corpus run on Fly (`performance`-2x / 4 GB, iad,
co-located with Turso aws-us-east-1), 5.9M actors:

```
total wall             1534 s  (~25.6 min)
peak RSS               406 MB  (VmHWM; bounded — NOT corpus-linear)
peak disk              24.2 GB (staging.db 15.6 + index.db 8.6; volume was 40 GB)
final index.db         8.58 GB  (real data; ships as-is, no free pages)
actors / emissions     5,903,058 / 144,070,520
distinct prefix keys   77,677,899  (prefix_top_base rows)

phase ms: turso_fetch 269k · derive 48k · actor_insert 15k
          emission_insert 96k · index_build 561k · group_pack 239k
          · analyze 232k
```

The dominant phases are now **CREATE INDEX (~9.4 min)** building the covering
staging index, then **ANALYZE (~3.9 min)** over the 8.6 GB artifact. group/pack
is only ~4 min.

Three fixes got us here, each found by a full-scale run:

1. **Memory** (`temp_store=MEMORY` → `FILE`): the `CREATE INDEX` sort over
   ~144M rows ran in RAM → **6.18 GB** peak at full scale. Spilling to the
   volume via `SQLITE_TMPDIR` drops it to a flat ~`cache_size` (**406 MB**,
   bounded regardless of corpus). This is THE knob that sizes the machine —
   a 1 GB machine runs this.
2. **Disk / artifact** (emissions staging → separate `staging.db`): kept in
   `index.db`, the ~11 GB of emissions+index left free pages that `DROP
   TABLE` without `VACUUM` never reclaimed → a ~12 GB artifact of mostly
   garbage. A throwaway attached `staging.db` we delete means `index.db`
   ships at real size (8.58 GB) with no VACUUM.
3. **group/pack I/O** (non-covering → covering index): the `(key, score)`
   index forced a rowid lookup into the ~8 GB emissions table for `did` on
   every one of 144M rows. At full scale that random I/O ran **4+ hours and
   did not finish**. Adding `did` to the index (`USING COVERING INDEX`,
   index-only sequential scan) cut it to **~4 min** (60×+). The covering
   index roughly doubles `staging.db` (it now carries `did`) — a cheap
   trade since staging is deleted.

Cadence target: **every 6-12 hours** — a ~26 min offline build fits easily.
The 8.58 GB artifact is the search service download/volume-sizing input.

### volume sizing per machine

Earlier "10 GB volume is fine" was wrong. search service and the indexer each
need to hold MULTIPLE snapshot copies plus runtime DB state during
promotion. Required minimums:

```
search service (typeahead-search.app machines):
  active snapshot:         ~7 GB
  incoming .part download: ~7 GB  (transient, during promotion)
  local.db (actors+overlay): ~4 GB  (currently 3.7 GB, grows slowly)
  WAL / tmp / overhead:    ~1 GB
  peak during promotion:   ~19 GB
  steady state:            ~12 GB
  TARGET VOLUME:           20 GB  (10 GB → 20 GB before first
                                    real-scale build)

Indexer machine — peak disk is DURING a build, from coexisting scratch:
  source.db (export):      ~3.2 GB  (deleted right after emissions phase)
  staging.db:              ~11 GB   (emissions ~8 + its index ~3; deleted at end)
  temp_store=FILE scratch: ~3 GB    (CREATE INDEX sort spill, transient)
  index.db (in progress):  up to ~7 GB  (real artifact)
  ─ phased, not all at once: emissions-phase peak ≈ source+staging+index
    ≈ 13 GB; index-phase peak ≈ staging+tmp+index ≈ 16 GB.
  retained previous artifact (rollback target): ~7 GB
  WAL / overhead:          ~1 GB
  PEAK DURING BUILD:       ~24 GB
  TARGET VOLUME:           30 GB   (measure actual peak on the first Fly run)
```

### cleanup policy

Snapshots are immutable but multi-versioned on disk. Without a cleanup
policy, stale files fill the volume — this is the next disk-full
incident waiting to happen.

**Indexer-side cleanup** (`typeahead-search.indexer` machine):

> NOT YET IMPLEMENTED — lands with manifest/promotion. The builder today
> only cleans within its own build: source.db deleted after the emissions
> phase, staging.db deleted at the end (both on success). The cross-build
> sweep below is manifest-aware, so it can't be written until the manifest
> exists — implementing a naive "delete all build-*" sweep now would delete
> the active snapshot once promotion lands. For the measurement-only first
> Fly run, start from a clean volume. A crashed build leaves its build dir
> (incl. a possible source.db.part) until the manifest-aware sweep ships.

- At the START of every build, sweep `/data/build-*/` directories that
  are not referenced by the current `manifest.json`'s `active_build` or
  `previous_build`. Failed/aborted prior builds leave partial dirs;
  this is where they get removed.
- After a successful build, the new manifest atomically promotes the
  new build_id as `active_build` and demotes the old `active_build` to
  `previous_build`. The build that was previously `previous_build` is
  now unreferenced and the next build's sweep deletes it.
- Crash recovery: if `manifest.json` is corrupt or absent, the indexer
  refuses to promote a new build until manually fixed — better than
  guessing. Operator runs the indexer once with `--manifest-init` (or
  similar) to bootstrap.

**App-side cleanup** (`typeahead-search.app` machines):

- On boot, delete every file under `/data/index/downloads/*.part`
  (stale from a previous run's interrupted download).
- On boot, list `/data/index/build-*/` directories; keep only the one
  matching the locally-fetched `manifest.json`'s `active_build` and
  one prior (for rollback). Delete the rest.
- After a successful promotion, the old active becomes the new
  previous; the previous-previous becomes unreferenced and gets
  deleted at the next promotion.
- WAL files: SQLite's WAL on the local.db is auto-managed by sync.zig's
  TRUNCATE checkpoint cadence (see hardening commit). Snapshot DBs are
  read-only after promotion so they don't grow WAL.

**Disk monitoring**: both machines should log `df -h /data` results
hourly. The "ingester disk-full silently kills sync" project memory
note applies equally here — disk pressure is silent and catastrophic.

## the overlay updater

lives inside search service's existing sync loop (`services/src/db/sync.zig`).
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

- overlay is bounded between rebuilds. rebuild cadence 6-12h (see sizing block above),
  typical handle/display change rate is low — overlay stays small (single-
  digit MB at most).
- compaction (next build) starts from current actors table state + last
  applied turso watermark. it does not need to "consume" overlay rows —
  the new base already reflects everything, so the indexer just truncates
  the overlay tables at swap time (or atomically swaps to an empty
  overlay alongside the new base).

## serving path

### queries longer than PREFIX_MAX

The builder emits `h:`, `d:` and Latin `n:` keys up to `PREFIX_MAX` (16) bytes.
`queryKeys` cuts those three lookups to the same bound (`prefixCut`) and keeps
`e:` whole. When any ASCII run of the query is longer than the bound
(`queryExceedsPrefixMax`), the candidates a cut key returns are a superset of
the real matches, so `index_search.filterFullQuery` re-checks each hydrated row
against the whole query with `matchesFullQuery` — the same rules the keys
encode (handle prefix, exact handle, dot-boundary suffix, display run). The
filter runs after hydration over at most `HYDRATE_CAP` rows. `/debug/search`
reports `query_cut` and `full_query_dropped`.

Until 2026-09-18 the query path emitted the uncut key, so any query longer
than 16 characters returned nothing unless it was a whole handle (`e:`).
Smoke pins `typeahead.waow.te` (17 chars) against this.

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
  current local snapshot indefinitely. serving liveness MUST NOT depend
  on the indexer being up — the indexer is a batch job, the app
  machines own their own copy. staleness grows but service stays up.
  when indexer comes back, normal poll picks up new manifests.
- **indexer machine OOM mid-build**: build-<new>/ files are partial,
  not promoted (manifest.json still points at previous). next indexer
  run sweeps unreferenced build-<id>/ dirs.
- **app machine disk fills during snapshot download**: download fails,
  watcher retries next tick. monitor `/data` usage on app machines;
  current overhead ~10-20GB total.
- **sha256 mismatch on download**: discard the partial, retry. log +
  page if it keeps failing — never attach a file that failed checksum.

## manifest contract

The manifest is the one durable artifact whose shape is part of the
indexer↔serving interface. Both sides must agree on it forever, and
breaking changes require a `manifest_version` bump + serving-side
handling for both old and new shapes during transition.

```json
{
  "manifest_version": 1,
  "active_build": "b1779638898",
  "previous_build": "b1779635298",
  "promoted_at": 1779638902,
  "snapshot_url": "/snapshot/b1779638898/index.db",
  "byte_size": 6442450944,
  "sha256": "3a7b...e9c1",
  "source_watermark": 1779638800,
  "actor_count": 5894868,
  "prefix_count": 4123550,
  "normalize_version": "v1-ascii-latin1",
  "scoring_version": "v1-quality-score",
  "top_n_short": 300,
  "top_n_medium": 200,
  "top_n_long": 100,
  "overlay_top_n": 100,
  "hydrate_cap": 50,
  "builder_version": "<git sha>"
}
```

Required fields, written atomically by the indexer (`.tmp` then rename):

| field | purpose |
|---|---|
| `manifest_version` | breaking-change guard. v1 is the first shipped format. |
| `active_build` | which build-<id>/ dir holds the current snapshot |
| `previous_build` | last good build kept on disk for rollback (or `null` for first ship) |
| `promoted_at` | unix seconds the indexer atomically rewrote this file |
| `snapshot_url` | relative path the indexer's HTTP server serves the .db under |
| `byte_size` | exact length of the .db file in bytes |
| `sha256` | hex-encoded SHA-256 of the .db file contents |
| `source_watermark` | max(updated_at) the indexer saw in turso actors at build start |
| `actor_count` | count of rows in actor_compact_base in this build |
| `prefix_count` | count of rows in prefix_top_base in this build |
| `normalize_version` | mirrors `normalize.zig` VERSION at build time |
| `scoring_version` | mirrors `scoring.zig` VERSION at build time |
| `top_n_*`, `overlay_top_n`, `hydrate_cap` | config values stamped at build time |
| `builder_version` | git sha of the indexer binary that produced this build |

Indexer writes manifest.json LAST in the build sequence, after the .db
file is fully fsync'd and checksummed. search service reads it via HTTP, parses
all fields, applies the promotion gate (next section), and only then
fetches the .db.

## promotion gate

The serving side never accepts a snapshot it can't fully validate.
"Accept" means: download, verify, open, version-check, then atomically
swap the active ATTACH. Each step is a gate.

```
1. Read indexer's manifest.json over HTTP.
   Reject if: HTTP non-200, JSON parse fails, unknown manifest_version,
   active_build matches the App's currently-attached build (nothing to do).

2. Download snapshot_url to /data/index/downloads/<id>.db.part
   (separate dir from the active build-<id>/ to make stray .part files
   easy to sweep on startup).
   Reject if: HTTP non-200, byte count != manifest.byte_size,
   sha256 of downloaded bytes != manifest.sha256.
   On reject: delete the .part, retry next tick.

3. Open the downloaded file with sqlite3 (read-only). Validate it:
   - prefix_top_base, actor_compact_base, index_meta tables exist
   - index_meta has every required key
   - index_meta.normalize_version == own normalize.VERSION (else: reject)
   - index_meta.scoring_version == own scoring.VERSION (else: reject)
   - index_meta.actor_count >= floor(prev_actor_count * 0.5)
       (sanity floor: a build with half the actors is suspect.
        For the FIRST build, prev_actor_count = 0 and this is skipped.)
   - index_meta.prefix_count >= floor(prev_prefix_count * 0.5)
   On reject: delete the .db, log + page operator. Stay on previous
   build. NEVER promote a snapshot that fails this stage.

4. Move the .part to /data/index/build-<id>/index.db (atomic rename).
   Now it's an addressable local artifact.

5. ATTACH DATABASE on the new file under alias "idx_new".

6. Briefly hold the App's snapshot mutex, re-prepare the live queries
   against idx_new, atomically swap the active alias to point at idx_new,
   release the mutex.

7. DETACH the old snapshot. Leave the old build-<id>/index.db on disk
   as the rollback target until the NEXT promotion's step 7.

8. Sweep: delete every build-<id>/ dir except the current active and
   the now-renamed-to-previous one.
```

Rollback is just rewriting manifest.json to point `active_build` at
`previous_build` on the indexer side; search service picks it up on its next
manifest poll and runs the same gate sequence against the rollback
target. Versions in index_meta MUST match too (the rollback target
has its own normalize_version that the current binary either still
supports or doesn't).

Critical invariants:
- The active snapshot file is **never mutated in place**. Snapshots are
  immutable artifacts; promotion is a re-attach.
- Serving keeps running from the currently-attached snapshot at every
  step. The promotion gate is non-blocking for /search.
- If the gate rejects a snapshot for ANY reason, the App stays on the
  prior snapshot indefinitely. We'd rather serve a stale-but-correct
  index than a fresh-but-suspect one.

## migration

1. land builder binary + indexer mode behind a build flag. new tables
   are local-only to search service and the indexer machine — created
   idempotently by a Zig `index/schema.zig` module on first open. NOT
   a Zug migration (Zug is for Turso shared schema; these tables don't
   exist in Turso). search service's existing FTS path untouched.
2. run indexer once manually. validate sizes, query a few prefixes from
   the resulting snapshot via sqlite3 directly.
3. wire overlay updater into search service's sync loop behind env flag
   `INDEX_OVERLAY=1`. defaults OFF.
4. add `USE_PREFIX_INDEX=1` env flag to search service's search handler. when set,
   route to new path. defaults OFF.
5. turn `INDEX_OVERLAY=1` on in search service. let it run for an hour. confirm
   overlay row counts stay bounded.
6. smoke test via Option A (direct search service URL, public unaffected).
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

continuous updates come off the relay's firehose the ingester already consumes
(`com.atproto.sync.subscribeRepos`): `app.bsky.graph.follow` records arrive in
the same commit stream as the profile records, so this is a new record-type
branch in the ingester writing to a local `follows` table — not a new
subscription and not a worker round trip.

  NOTE (2026-08-12): this section originally routed follows through jetstream
  and the worker's /admin/ingest. That premise died on 2026-07-28 when we cut
  jetstream for the relay's firehose — jetstream is a silently partial projection
  and drops records the relay carries (see docs/discovery-and-identity.md).
  A follow graph built on it would be quietly incomplete, which is the worst
  possible input to an authority score.

history is missing for follows that pre-date our firehose subscription.
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
→ search service's sync replicates actors row updates
→ overlay updater detects score_changed prefixes → writes prefix_overlay
  with op=1 and the new score
→ next indexer rebuild (4-6h later) folds the new scores into prefix_top_base
```

no coordination needed. track 1 already plumbs `quality_score` through
the index. swap the score function under it and the index updates
automatically.

## backfill contract

Bulk / historical writes are first-class. The architecture absorbs them
without special-casing because the snapshot is fully regenerable and
the overlay tolerates re-ingestion.

Existing and future backfill paths:

| Path | Destination | Frequency | How the index sees it |
|---|---|---|---|
| `scripts/bulk-enrich.py` (getProfiles) | worker → Turso `actors` | one-shot / nightly | local sync picks up updated rows → overlay updater diffs prefixes → next rebuild folds into base |
| `scripts/plc-identity-sync.py` (PLC bundles → handle + pds) | worker → Turso `actors.handle`/`actors.pds` | one-shot | handle changes prefix keys, so the overlay re-derives them; pds is blob-only |
| AccountTakedown PDS-direct enrichment | worker → Turso `actors` | continuous (cron) | same path |
| follow graph backfill (Track 2) | ingester-local `follows` table | phased (sample → broad) | pagerank job reads `follows`, writes scores back to Turso `actors.quality_score` → same path as above |
| handle rename event | relay's firehose → ingester → Turso | continuous | overlay diff produces `op=0` rows for stale prefixes (set-diff via deriveAllUnique) |
| account deletion (tombstone) | relay's firehose → ingester → Turso | continuous | `actor_overlay.blob = NULL` tombstone + `prefix_overlay op=0` for every prefix the actor used to occupy |

**Invariants the backfill contract relies on:**

1. **Turso `actors` is source-of-truth for everything in the index.**
   Backfills always write there (via worker). Ingester-local artifacts
   (`follows` table, snapshot DB) are derived state.
2. **The overlay updater is idempotent.** Replaying the same actor
   change N times produces the same overlay state. Backfills can stream
   updates as fast as the worker can write; the overlay absorbs them.
3. **Snapshots regenerate from current Turso state.** Bad data corrected
   in Turso is automatically corrected in the next rebuild. No special
   "rebuild from scratch" or "wipe index" tool needed.
4. **Snapshots are NEVER backfilled in place.** A snapshot file is
   immutable from the moment its manifest is promoted. Corrections land
   in the next snapshot, gated by the promotion gate above.
5. **The follows table on the ingester volume CAN be backfilled in
   place** — it's append-only (or upsert) state, not a snapshot. Lost
   rows can be re-fetched from PDSes via listRepos.

Operational note: a large bulk-enrichment run will produce thousands of
overlay rows over the sync window before the next snapshot rebuild
absorbs them. The overlay tables are sized for that — `prefix_overlay`
is unbounded between rebuilds, and each rebuild compacts everything
back into the base. Monitor `prefix_overlay` row count in /health
during big backfills; if it ever exceeds a sane bound (say 1M rows),
trigger a rebuild manually.

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

1. **Japanese/CJK search — addressed in v2 (`normalize_version` =
   `v2-cjk-bigram`).** v1 dropped every codepoint outside ASCII + Latin-1
   + Latin Extended-A, so `佐藤` normalized to the empty string and emitted
   no `n:` keys. v2 preserves Han + Hiragana (folding katakana→hiragana +
   full-width ASCII), and the prefix tokenizer bigrams CJK runs into `n:`
   keys (+ a leading unigram). See **`docs/i18n-cjk-search.md`** for the
   design + Phase 0 sizing (~7.3% of display names are CJK; +0.7% build
   emissions). **Still unsearchable:** other non-Latin scripts (Arabic,
   Hebrew, Hangul, Thai…) and reading-based search (`さとう`→`佐藤`, needs
   furigana we don't store) — later versions.

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
services/src/index/normalize.zig
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

services/src/index/prefixes.zig
  pub fn derive(arena, actor) []PrefixKey
  pub const PREFIX_MAX_LEN: usize = 16

services/src/index/scoring.zig
  pub fn score_of(actor) f32
  -- v1: returns actor.quality_score directly
  -- v2 (later): may incorporate recency, mod signals, etc.

services/src/index/pack.zig
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

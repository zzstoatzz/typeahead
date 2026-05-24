# turso capabilities — features we haven't been using

Research artifact for soliciting codex review on Track 1 (indexer + serving
path) and Track 2 (follow graph + pagerank) decisions, with the Turso
features we've been ignoring laid out explicitly.

This is *not* an architecture decision doc. It's the menu of options we
just learned exist. Decisions live in `docs/typeahead-index-design.md`.

## starting context

- We're on the **Scaler plan** ($30/mo, upgraded 2026-05-24). Quotas are
  effectively non-binding for this workload:
  - 100B row reads / month
  - 100M row writes / month
  - 24 GB storage
  - 24 GB sync GB / month
  - 30-day point-in-time recovery
  - 25ms commit latency budget
- We TALK to Turso from Zig via hrana **v2** pipeline (HTTP, JSON-encoded
  request bodies, manually serialized in `ingester/src/db/TursoClient.zig`).
- We bind args as `text` or `integer` only (see `RuntimeValue` tagged union).
  Missing: `blob`, `float`, `null` is supported.
- The worker (Cloudflare) talks to Turso via the libsql JS client.
- The indexer (new, Zig) currently uses keyset pagination
  (`did > ? ORDER BY did LIMIT 2000`) because hrana v2 has no streaming /
  cursor. Per-build Turso fetch was 91% of total build time at 100k
  actors in pre-pagination tests.

## what Turso supports that we don't use

### tier 1 — could obsolete current work

**1. `turso db export` for full-snapshot pulls**
[cli/db/export](https://docs.turso.tech/cli/db/export)

```
turso db export typeahead /tmp/turso-snapshot.db
```

Produces a literal sqlite3 file containing the entire DB. The indexer's
"stream rows from Turso via HTTP" phase — the bottleneck we just spent
a commit paginating around — becomes a single subprocess call plus
`ATTACH DATABASE` locally.

What it would change in our code:
- Replace `streamActorsAndEmit`'s pagination loop with: shell out
  to `turso db export`, `ATTACH "/tmp/turso-snapshot.db" AS source`,
  then `SELECT … FROM source.actors`.
- Pagination scaffolding becomes dead code. Source-watermark filter
  still applies (filter to `updated_at <= ?` in the local SELECT).
- Indexer machine no longer needs hrana credentials for the read path —
  just `turso` CLI authed.

Open question: how fast is `db export` at 5.9M rows on the Scaler tier?
We have not measured.

**2. embedded replicas on Fly**
[features/embedded-replicas/introduction](https://docs.turso.tech/features/embedded-replicas/introduction),
[with-fly](https://docs.turso.tech/features/embedded-replicas/with-fly)

A local libsql `.db` file kept in continuous sync with Turso cloud via
the `sync()` API or `syncInterval` (seconds). Requires a writable
filesystem (Cloudflare Workers no, Fly yes — `/data` already there).
Our memory file says "embedded replicas need a filesystem — CF Workers
don't have one" — true for the worker, false for the indexer/app B
which live on Fly.

What it would change:
- The ingester and indexer read from the local libsql file. Zero HTTP
  for reads. The sync subsystem handles deltas in the background.
- The current `sync.zig` pipeline (Turso fetch → batch insert into
  local sqlite) becomes redundant — libsql does it natively.
- `App B serving` already reads from `LocalDb` (zqlite over stock
  libsqlite3); switching the underlying storage to a libsql embedded
  replica means changing the SQLite driver in the ingester.

Engineering cost:
- libsql is a Rust fork of SQLite with C bindings. We currently use
  `karlseguin/zqlite.zig` over stock libsqlite3. Switching means either
  a Zig wrapper around libsql's C interface (libsql-c) or a Rust
  sidecar that owns the embedded replica and exposes it via a Unix
  socket. Non-trivial.

Critical note from research:
- Auto-checkpoint is DISABLED for sync DBs. Must call `checkpoint()`
  manually after bulk ops, or the local WAL grows unbounded
  ([sync/checkpoint](https://docs.turso.tech/sync/checkpoint)).
- Partial sync is page-level, not row-level
  ([sync/partial](https://docs.turso.tech/sync/partial)).

**3. hrana v3 cursors**
[HRANA_3_SPEC](https://github.com/tursodatabase/libsql/blob/main/docs/HRANA_3_SPEC.md)

Our `TursoClient.zig` is on `/v2/pipeline` which only supports `execute`
(no streaming). Hrana v3 adds:

- `open_cursor` / `fetch_cursor` / `close_cursor` — server-side
  streaming cursor. Eliminates our keyset-pagination workaround.
- `batch` — atomic multi-statement transactions in one request.
- `sequence` — multi-statement scripts.
- `store_sql` / `close_sql` — prepared SQL by id; stop re-sending the
  same INSERT/SELECT text on every batch.
- `describe` — schema introspection.
- `get_autocommit`.

What it would change:
- The indexer's batched fetch becomes a single open_cursor + repeated
  fetch_cursor.
- The cursor-position concept lives server-side, so a crash mid-build
  could potentially resume (depending on cursor lifetime semantics —
  needs investigation).
- `TursoClient.zig` URL changes from `/v2/pipeline` to `/v3/pipeline`
  and request shapes get more variants.

This is incremental — same JSON protocol, same auth, just more verbs.

### tier 2 — orthogonal future leverage

**4. native vector types**
[features/ai-and-embeddings](https://docs.turso.tech/features/ai-and-embeddings),
[sql-reference/functions/vector](https://docs.turso.tech/sql-reference/functions/vector)

Six element dtypes: F64, F32, F16, FB16, F8, F1BIT. Max 65,536 dims.
DiskANN ANN index syntax exists: `CREATE INDEX foo_idx ON foo(libsql_vector_idx(embedding))`.
Distance functions: `vector_distance_cos / _l2 / _dot / _jaccard`.
Query function: `vector_top_k(...)`.

**Important caveat**: the SQL reference page explicitly says
> Vector indexes are not yet supported. All similarity searches use a
> linear scan over the table.

The feature page and AI guides describe `libsql_vector_idx` as if it
works. Need to verify before committing the schema.

Possible application orthogonal to PageRank:
- Embed each actor's handle + display name with a small model.
- Store as `F8_BLOB(128)` — 1 byte per dim, 128 dims, 5.9M actors
  ≈ 750 MB.
- For each query: get FTS candidate set (~few hundred), compute
  `vector_distance_cos(query_embedding, candidate.embedding)`, re-rank.
- Linear scan over candidates is sub-ms even without ANN. Linear scan
  over the full corpus would be slow but we never do that for the
  request path.

This is exploratory. Not blocking anything.

**5. jsonb + expression indexes**
[sql-reference/functions/json](https://docs.turso.tech/sql-reference/functions/json),
[sql-reference/statements/create-index](https://docs.turso.tech/sql-reference/statements/create-index)

Turso supports full sqlite json1 plus `jsonb_*` variants and the
`->` / `->>` operators. Expression indexes are supported.

What we could change:
- Migrate `actors.labels` from TEXT to `jsonb` (~30% smaller).
- `CREATE INDEX actors_no_unauth ON actors(json_extract(labels, '$.no_unauth'))`
  for moderation queries that today scan and `LIKE` the JSON text.
- The Zig builder's `pack.zig` currently treats `labels` as an opaque
  string blob. Could remain opaque (we just pass through), or evolve
  to typed parsing.

Touchpoints: Turso schema migration (zug 003 territory), worker
write paths, ingester sync. Real lift.

**6. Change Data Capture (CDC)**
[tursodb/cdc](https://docs.turso.tech/tursodb/cdc)

`PRAGMA capture_data_changes_conn('full')` writes insert/update/delete
events to a `turso_cdc` table. Subscribers can poll this for changes.

Application for Track 2:
- Incremental PageRank could subscribe to `follows` / `actors` deltas
  via `turso_cdc` instead of re-scanning the world every job tick.
- This is a structural alternative to the "scan everything every 24h"
  pattern in `indigo/cmd/palomar/pagerank.sh` that bsky uses.

Mutual exclusion: CDC and MVCC (concurrent writes) are mutually
exclusive on the same DB. We aren't using MVCC, so this is fine.

**7. native typed binds we're missing**

Hrana arg encoding supports `null`, `integer`, `float`, `text`, and
`blob (base64)`. Our `RuntimeValue` tagged union has `text` / `int` /
`null` only.

Adding `float` and `blob` to `RuntimeValue` is a small change to
`TursoClient.zig`'s `buildTypedRequestBody`. Lets us:
- Bind `quality_score` as REAL when writing actor enrichment.
- Bind packed binary payloads directly without hex-encoding.

This is a paper cut, not a blocker.

### tier 3 — don't pursue (with rationale)

**native Tantivy-backed FTS** (`CREATE INDEX ... USING fts` with
`tokenizer='ngram'`) — we are *moving away* from FTS toward the
precomputed prefix index. Switching to a different FTS engine is
the opposite direction of travel.

**Multi-DB schemas** — deprecated per Turso docs. Skip.

**Branching for green/blue snapshot rebuild** — interesting but our
snapshot-file-with-manifest-promotion model already provides atomic
swap. Branching would be a different paradigm, not an additive feature.

### important correction to our existing code

**`busy_timeout` and `journal_mode` pragmas are no-ops on Turso Cloud**
([cloud/limitations](https://docs.turso.tech/cloud/limitations)).

We set these in `TursoClient`-using code paths sometimes. They only
matter for the LOCAL SQLite (snapshot DB, App B's `local.db`, the
indexer's emissions staging) — never for the actual Turso connection.
This is mostly a comment correction; we aren't setting them where it
matters, but it's worth noting so future code doesn't mistakenly try.

## quota reality check (Scaler tier)

| metric | Scaler limit | our usage | % of budget |
|---|---|---|---|
| Row reads / mo | 100 B | full-corpus indexer rebuild = 5.9M; 4 rebuilds/day = 700M/mo | 0.7% |
| Row writes / mo | 100 M | jetstream ingest + bulk-enrich + admin writes; estimate ~10M/mo | 10% |
| Storage | 24 GB | actors table is ~4 GB today; follows table at full scale could be ~5-15 GB | fits |
| Sync GB / mo | 24 GB | only matters if we adopt embedded replicas (§2) | n/a today |
| Commit latency budget | 25 ms | the worker's writes already need to be fast; this is a real constraint | tight |

Implication: **full-corpus rebuilds are now essentially free** at the
Scaler tier. We can run the indexer as often as we want without quota
pressure. PageRank read-side will be free too.

## recommended next moves (for codex to weigh in on)

In rough order of leverage-per-effort:

**A. measure `turso db export` performance at 5.9M actors.**
One subprocess call. If it's under a minute, the indexer's fetch
phase shrinks from "minutes of HTTP" to "seconds of dump + ATTACH",
and the pagination work we just committed becomes a safety-net
fallback rather than the primary path.

**B. add `float` and `blob` to `RuntimeValue` in `TursoClient.zig`.**
Trivial change. Unblocks future code that needs REAL or BLOB binds
without per-call hex encoding.

**C. switch builder to typed binds for the paginated query.**
Already discussed; converts watermark/cursor/LIMIT from text-coerced
to native types. Low risk, marginal correctness win.

**D. investigate hrana v3 cursors for `TursoClient.zig`.**
Bigger lift. Would supersede pagination entirely with a
streaming-cursor primitive. Same protocol family, same auth, just
more verbs. Useful for the bulk-enrichment scripts too.

**E. evaluate embedded replicas as a longer-term shape.**
The largest lift (libsql C bindings into Zig, or a Rust sidecar)
but the cleanest architectural answer to "indexer/serving never
touches HTTP for reads." Worth a design exploration even if we
don't commit to it now.

**F. punt vector embeddings + CDC + jsonb to separate threads.**
All real wins, none blocking, all best handled as focused projects
after Track 1 lands.

## what we should NOT do

- Don't pivot the in-flight precomputed-prefix-index architecture to
  use Turso's native FTS — same direction-of-travel argument.
- Don't drop the keyset pagination commit even if `db export` is fast.
  It's a working fallback and isn't blocking anything.
- Don't put `follows` (Track 2 graph) in Turso. Local-only storage on
  the indexer/pagerank machine matches our existing memory note about
  Turso row pricing at follow scale, even on Scaler.

## sources

- https://docs.turso.tech/sitemap.xml
- https://docs.turso.tech/sql-reference/data-types
- https://docs.turso.tech/sql-reference/functions/vector
- https://docs.turso.tech/features/embedded-replicas/introduction
- https://docs.turso.tech/features/embedded-replicas/with-fly
- https://docs.turso.tech/sync/usage
- https://docs.turso.tech/sync/checkpoint
- https://docs.turso.tech/sync/partial
- https://docs.turso.tech/sql-reference/functions/json
- https://docs.turso.tech/sql-reference/statements/create-index
- https://docs.turso.tech/tursodb/cdc
- https://docs.turso.tech/cli/db/export
- https://docs.turso.tech/cloud/limitations
- https://github.com/tursodatabase/libsql/blob/main/docs/HRANA_3_SPEC.md
- https://turso.tech/pricing

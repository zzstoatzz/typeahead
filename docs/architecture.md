# architecture

community actor search for atproto. indexes the network via jetstream
and serves FTS5 prefix search from cloudflare's edge.

## data flow

```
  jetstream (JSON over WebSocket)
       |
       v
  ingester (zig, fly.io)
       |  bloom filter dedup (~1.2MB fixed)
       |  two-tier writes:
       |    - bare DID: INSERT OR IGNORE (0 Turso writes for known actors)
       |    - profile/identity event: full UPSERT with COALESCE
       |  dual-write to local SQLite (profile/identity events)
       |
       +---> worker (cloudflare) ---> Turso (source of truth)
       |                         ---> KV (cursor, mod_cursor, enrich_lock)
       |
       +---> local SQLite replica (/data/local.db on fly volume)
                synced from Turso on startup + every 5 min
                serves search queries directly (FTS5)
```

### search read path

```
  search request
       |
       v
  CF worker ---> cache API (60s edge cache, hit?)
       |                         |
       | miss                    | hit → return cached
       v
  fly ingester /search (local SQLite, 50-150ms cold)
       |
       | timeout/error (3s)
       v
  Turso fallback (CF worker queries Turso directly)
```

the ingester's `/search` uses a 3-tier ranking strategy (matching the
worker's Turso fallback):

1. **exact handle** — `WHERE handle = ? COLLATE NOCASE`, instant via index
2. **handle prefix** (two-phase) — phase 1: `WHERE handle LIKE ? LIMIT N`
   with no `ORDER BY` (index-friendly, early termination), overfetch 5x
   into a stack buffer. phase 2: insertion-sort candidates by handle length
   in zig, then point-lookup by DID for full rows. restores the worker's
   shortest-handle-first ranking without the pathological full-scan sort
   that `ORDER BY length(handle)` causes on broad prefixes.
3. **FTS5 prefix** (two-phase, skipped if tiers 1+2 fill the limit or
   `term.len < 2`) — phase 1: `SELECT did FROM actors_fts WHERE MATCH ?
   ORDER BY rank LIMIT N` (pure FTS5, no JOIN — enables rank optimization
   with priority queue + early termination). phase 2: point-lookup each
   candidate DID on the actors table, filter `hidden = 0` and
   `handle != ''`.

the two-phase approach avoids two pathological query shapes:
- tier 2: `ORDER BY length(handle)` forces SQLite to scan all LIKE matches
  before sorting — catastrophic for short prefixes like `a%` (100k+ rows)
- tier 3: `JOIN actors ON actors.did = actors_fts.did` prevents FTS5's
  native rank optimization — SQLite must scan all FTS matches, JOIN each,
  filter, sort, then take top N

**caveat**: the ingester has a single `read_conn` shared across the HTTP
thread pool. one slow query blocks all concurrent reads. the two-phase
strategy bounds individual query time, but read concurrency isolation
(connection pool or per-request connections) is a longer-term fix.

### write paths

1. **ingester**: streams jetstream (profiles, posts, likes, follows), buffers
   actor events, POSTs batches to `/admin/ingest`. sends avatar CIDs (not full
   URLs — the CDN URL is deterministic from DID + CID). bloom filter dedup
   prevents re-sending already-known bare DIDs.

2. **enrichment pipeline** (dispatched from ingest hot path via `waitUntil`):
   - **phase 1 — identity**: slingshot resolves DID → handle + PDS endpoint.
     100 DIDs/run, 20 concurrent. attempt tracking (`identity_checked_at`)
     backs off failures for 1 hour.
   - **phase 2 — profile + labels + metadata**: `getProfiles` batch call
     (25 DIDs/call, 75/run) fetches avatar, display_name, labels, createdAt,
     and associated in one shot. also recomputes `hidden` from full label
     data. attempt tracking (`profile_checked_at`) backs off failures for
     1 hour. actors not returned by getProfiles are marked to avoid retrying.
   - lease-coordinated via KV (`enrich_lock`, 30s TTL) — no stampede from
     overlapping ingest batches.
   - converges to zero work as gaps fill (gap-driven queries return fewer
     results over time).

3. **backfill**: when a search has gaps (missing avatars, few results), the
   worker calls bluesky's `searchActorsTypeahead` API and upserts results.
   extracts CIDs from the full avatar URLs returned by the API.

4. **request-indexing**: POST `/request-indexing?handle=...` resolves via
   slingshot, fetches profile from bluesky public API, extracts avatar CID,
   checks moderation labels, upserts.

5. **hourly cron**:
   - records actor count snapshot (total, with handles, with avatars) —
     serves as reconciliation anchor for delta-based tracking
   - refreshes moderation labels (walks 1000 actors/run via `mod_cursor`);
     piggybacks enrichment — saves handle, display_name, avatar from the
     getProfiles response at zero additional API cost
   - runs `enrichActors` as a catch-up for idle periods (same function,
     same lease — no overlap risk)

### metrics

- **search metrics**: 5-min buckets in `metrics` table (searches + total_ms)
- **actor deltas**: 5-min buckets in `actor_deltas` table tracking incremental
  changes to actors, handles, and avatars counts. recorded at write time
  (ingest, delete, enrichment) via fire-and-forget `waitUntil`.
- **hourly snapshots**: absolute counts from `COUNT(*)` queries, used as
  reconciliation anchors. the trend chart stitches snapshots + deltas for
  sub-hourly resolution between anchor points.
- **traffic sources**: cumulative hit counters per client identity (X-Client >
  Origin > Referer). loopback/localhost normalized to "unknown". internal
  traffic (bench script) identified via `X-Client: bench` and excluded from
  the stats page pie chart.

### enrichment convergence

the enrichment pipeline is gap-driven:

- identity phase queries actors with `handle = ''` and
  `identity_checked_at < now - 1hr`
- profile phase queries actors with `handle != ''` and
  (`avatar_url = ''` OR `labels = '[]'` OR `created_at = ''`),
  `profile_checked_at < now - 1hr`
- as actors get enriched, these queries return fewer rows
- system quiesces when all actors are resolved or backed off

AppView (public.api.bsky.app) is used for both enrichment (phase 2
getProfiles) and moderation label checks (hourly cron). phase 2 gets
avatar, displayName, labels, createdAt, associated, and hidden in one
batch call.

### local replica sync

the ingester maintains a local SQLite replica on a fly.io persistent volume,
synced from Turso. search queries hit this replica directly — no round-trip
to Turso for reads.

- **bootstrap** (first run or wipe): stage-and-swap — loads all actors into
  staging tables with no indexes, builds indexes in bulk (`CREATE INDEX` on
  unindexed data), populates FTS5, then atomically swaps staging tables into
  place via `ALTER TABLE RENAME`. temp files pinned to `/data/tmp` on the
  volume. read connection closed during bootstrap to avoid WAL interference.
- **re-sync** (restart with existing data): keeps serving while re-syncing
  from Turso. updates the live indexed table directly — index maintenance
  cost is negligible for incremental changes.
- **incremental** (every 5 min): keyset pagination on `(updated_at, did)`,
  tombstone + unsearchable cleanup. only advances watermark when all
  queries succeed.
- **dual-write**: profile and identity events from jetstream are written
  directly to the local replica for immediate search availability (when
  `ready=true`).

## storage

the actors table stores one row per DID:

    did TEXT PRIMARY KEY          -- ~32 bytes
    handle TEXT                   -- ~25 bytes
    display_name TEXT             -- ~20 bytes
    avatar_url TEXT               -- ~59 bytes (CID only, URL reconstructed at query time)
    hidden INTEGER                -- 1 byte
    labels TEXT                   -- 2 bytes ('[]') for ~92% of actors;
                                  -- ~256 bytes per label for the ~8% that have them
                                  -- stored as raw JSON array matching bsky's API shape
    created_at TEXT               -- ~24 bytes (ISO 8601 timestamp from getProfiles)
    associated TEXT               -- 2 bytes ('{}') until enriched; ~100 bytes when populated
                                  -- stored as JSON matching bsky's profileAssociated shape
    updated_at INTEGER            -- 8 bytes
    pds TEXT                      -- ~40 bytes (PDS endpoint URL)
    identity_checked_at INTEGER   -- 8 bytes (last slingshot attempt)
    profile_checked_at INTEGER    -- 8 bytes (last PDS profile attempt)

plus FTS5 index overhead. roughly ~350 bytes/row for unlabeled actors,
~610 bytes/row for labeled ones (~8% of index). `created_at` and
`associated` populate progressively as the enrichment pipeline and
moderation cron cycle through actors — no separate backfill needed.

storage is [Turso](https://turso.tech) (hosted libSQL). previously used
Cloudflare D1 but migrated to Turso to avoid D1's 10GB hard limit and
per-tenant design constraints that made sharding a global FTS5 index
impractical.

## moderation

actors with certain labels from bluesky's moderation service
(`did:plc:ar7c4by46qjdydhdevvrndac`) are hidden from search results
(`hidden = 1`): `!hide`, `!takedown`, or `spam`.

`!no-unauthenticated` is intentionally **not** filtered — it applies to
content, not identity. bluesky's own public typeahead API returns these
accounts, and so do we.

the full labels array (matching bsky's API shape) is stored per actor and
returned in search results. clients can use this for rendering decisions
(e.g. blur avatars for `porn`/`sexual` labels). the ingester only sees
self-labels from profile records — it cannot see bsky mod labels, so it
does not set `hidden`. only API-based paths (cron, backfill, request-indexing)
set `hidden` from full label data.

the hourly cron refreshes moderation labels and persists the full labels
array by walking the full index over multiple runs (1000 actors/run via
`mod_cursor` in KV). it piggybacks enrichment data (handle, display_name,
avatar) from the same getProfiles response at no additional API cost.

## scripts

- `scripts/bench.py` — latency, coverage, and stress benchmarks against prod
  (sends `X-Client: bench` to avoid polluting traffic stats)
- `scripts/smoke.py` — end-to-end smoke tests against a live deployment
- `scripts/backfill-moderation.py` — one-shot sweep to set hidden flags on
  existing actors (run once after adding moderation support)
- `scripts/add-enrichment-columns.sql` — one-shot migration adding pds,
  identity_checked_at, profile_checked_at columns (run once before deploying
  enrichment pipeline)
- `scripts/add-labels-column.sql` — one-shot migration adding labels column
  to actors table (run once before deploying labels support)
- `scripts/backfill-profiles.py` — bulk enrichment: walks actors table and
  populates labels, handle, display_name, avatar_url via getProfiles API.
  also recomputes hidden from full label data, fixing actors incorrectly
  hidden by stale !no-unauthenticated logic.
- `scripts/add-created-associated.sql` — one-shot migration adding created_at
  and associated columns to actors table
- `scripts/add-actor-deltas.sql` — one-shot migration adding actor_deltas
  table for 5-min granularity delta tracking
- `scripts/migrate-avatar-cid.sql` — one-shot migration from full avatar
  URLs to bare CIDs (already applied to production)
- `scripts/migrate-to-turso.py` — one-shot D1-to-Turso data migration
  (already applied to production)

## notes

- `docs/notes/ingester-oom-firehose-widening.md` — postmortem from widening
  jetstream subscriptions to posts/likes/follows, causing ingester OOM on
  256MB fly.io VM. root cause: in-memory dedup set with retain-capacity
  semantics. resolved with bloom filter approach.

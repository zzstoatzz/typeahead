# architecture

community actor search for atproto. indexes the network via jetstream
and serves FTS5 prefix search from cloudflare's edge.

## data flow

```
  jetstream (JSON over WebSocket)
       |
       v
  ingester (zig, fly.io)
       |  batches of {did, handle, display_name, avatar_cid, hidden}
       v
  worker (cloudflare)
       |
       +---> D1 (actors table + FTS5 index)
       +---> KV (cursor, config flags, mod_cursor)
       +---> cache API (60s edge cache for search)
```

### write paths

1. **ingester**: streams jetstream, buffers actor events, POSTs batches to
   `/admin/ingest`. sends avatar CIDs (not full URLs — the CDN URL is
   deterministic from DID + CID). detects `!no-unauthenticated` self-labels
   and sets `hidden` flag.

2. **backfill**: when a search has gaps (missing avatars, few results), the
   worker calls bluesky's `searchActorsTypeahead` API and upserts results.
   extracts CIDs from the full avatar URLs returned by the API.

3. **request-indexing**: POST `/request-indexing?handle=...` resolves via
   slingshot, fetches profile from bluesky public API, extracts avatar CID,
   checks moderation labels, upserts.

4. **hourly cron**:
   - records actor count snapshot (total, with handles, with avatars)
   - refreshes moderation labels (walks 1000 actors/run via `mod_cursor`)
   - resolves missing handles via slingshot (up to 1000/run)

### read path

search query -> cache API (hit?) -> FTS5 prefix match -> reconstruct avatar
URLs from DID + CID -> return `{did, handle, displayName?, avatar?}`

## storage

the actors table stores one row per DID:

    did TEXT PRIMARY KEY          -- ~32 bytes
    handle TEXT                   -- ~25 bytes
    display_name TEXT             -- ~20 bytes
    avatar_url TEXT               -- ~59 bytes (CID only, URL reconstructed at query time)
    hidden INTEGER                -- 1 byte
    updated_at INTEGER            -- 8 bytes

plus FTS5 index overhead. roughly ~280 bytes/row total.

D1 has a 10GB hard limit (non-negotiable). at current row size that's ~35M
actors. D1 is designed for per-tenant databases, not single large datasets —
sharding a global search index across multiple D1s degrades FTS5 ranking
(scores aren't comparable across shards) and fans out every query.

when we outgrow D1, the natural move is **Turso** (hosted libSQL). our sibling
project [leaflet-search](https://tangled.org/zzstoatzz.io/leaflet-search)
already runs Turso + local SQLite read replica in production for FTS5 search.
the schema and queries would port with minimal changes.

## moderation

actors with certain labels are hidden from search results (`hidden = 1`):

- bluesky moderation service (`did:plc:ar7c4by46qjdydhdevvrndac`) issued
  `!hide`, `!takedown`, or `spam`
- any source (including self-labels): `!no-unauthenticated`

this is an unauthenticated service, so we respect user opt-outs. the ingester
catches self-labels on ingest; the hourly cron refreshes moderation labels
by walking the full index over multiple runs.

## scripts

- `scripts/smoke.py` — end-to-end smoke tests against a live deployment
- `scripts/backfill-moderation.py` — one-shot sweep to set hidden flags on
  existing actors (run once after adding moderation support)
- `scripts/migrate-avatar-cid.sql` — one-shot migration from full avatar
  URLs to bare CIDs (already applied to production)

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
       +---> Turso (actors table + FTS5 index)
       +---> KV (cursor, mod_cursor)
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

storage is [Turso](https://turso.tech) (hosted libSQL). previously used
Cloudflare D1 but migrated to Turso to avoid D1's 10GB hard limit and
per-tenant design constraints that made sharding a global FTS5 index
impractical.

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
- `scripts/migrate-to-turso.py` — one-shot D1-to-Turso data migration
  (already applied to production)

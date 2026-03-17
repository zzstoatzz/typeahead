# typeahead

community actor search for [atproto](https://atproto.com).

**live:** https://typeahead.waow.tech

## what it does

drop-in replacement for Bluesky's `app.bsky.actor.searchActorsTypeahead` endpoint. any atproto app can point at this instead of `public.api.bsky.app` and get actor search without depending on Bluesky's infrastructure.

```bash
curl "https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=nate&limit=10"
```

## how it works

1. **ingester** (zig) subscribes to [Jetstream](https://docs.bsky.app/blog/jetstream), filters identity and profile events, batches them to the worker
2. **worker** (cloudflare) serves search via FTS5 full-text search over D1, with edge caching (60s) and rate limiting
3. **backfill** fills index gaps by querying Bluesky's typeahead in the background — temporary bridge while the index catches up

## stack

- **ingester**: zig on fly.io — streams jetstream, posts batches to worker
- **worker**: cloudflare worker + D1 (sqlite/FTS5) + KV + cache API
- **search**: FTS5 prefix matching, edge-cached per query

## run locally

```bash
# worker
npm install
npx wrangler dev

# ingester
cd ingester && zig build run
```

requires `TYPEAHEAD_URL` and `TYPEAHEAD_SECRET` env vars for the ingester.

## deploy

```bash
npx wrangler deploy                    # worker to cloudflare
cd ingester && fly deploy              # ingester to fly.io
```

## known limitations

- **response shape**: returns `did`, `handle`, `displayName`, `avatar`. does not yet include `associated`, `labels`, `createdAt`, or other fields from [`profileViewBasic`](https://docs.bsky.app/docs/api/app-bsky-actor-search-actors-typeahead). apps that only destructure the core fields work fine; apps that depend on labels or associated metadata should be aware of this gap.
- **no moderation filtering**: results are not filtered by moderation labels. unsafe accounts may appear in results.
- **index coverage**: the index grows from jetstream events + backfill. it does not yet have full coverage of all atproto actors.

## license

MIT

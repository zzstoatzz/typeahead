# typeahead

community actor search for [atproto](https://atproto.com). drop-in replacement for `app.bsky.actor.searchActorsTypeahead`.

**live:** https://typeahead.waow.tech

```bash
curl "https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=nate&limit=10"
```

## stack

```
jetstream → ingester (zig, fly.io) → worker (cloudflare)
                                        ↓
                                   D1 (sqlite/FTS5)
```

- **ingester**: [zig](https://ziglang.org) on [fly.io](https://fly.io) — streams identity + profile events via [jetstream](https://docs.bsky.app/blog/jetstream), batches to worker
- **worker**: [cloudflare worker](https://workers.cloudflare.com) + D1 + KV + cache API — FTS5 prefix search, edge-cached (60s), rate-limited
- **identity**: [slingshot](https://microcosm.blue) for on-demand handle resolution

## dev

```bash
# worker
npm install && npx wrangler dev

# ingester
cd ingester && zig build run
```

requires `TYPEAHEAD_URL` and `TYPEAHEAD_SECRET` env vars for the ingester.

## deploy

```bash
npx wrangler deploy              # worker to cloudflare
cd ingester && fly deploy        # ingester to fly.io
```

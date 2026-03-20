# typeahead

community actor search for [atproto](https://atproto.com). aims to be a drop-in replacement for `app.bsky.actor.searchActorsTypeahead` — same endpoint path and query params, returns the full `profileViewBasic` surface minus `viewer` (`did`, `handle`, `displayName`, `avatar`, `associated`, `labels`, `createdAt`).

**live:** https://typeahead.waow.tech

```bash
curl "https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=nate&limit=10"
```

## stack

```
jetstream → ingester (zig, fly.io) → worker (cloudflare)
                                        ↓
                                   Turso (libSQL/FTS5)
```

- **ingester**: [zig](https://ziglang.org) on [fly.io](https://fly.io) — streams profiles, posts, likes, and follows via [jetstream](https://docs.bsky.app/blog/jetstream), batches to worker
- **worker**: [cloudflare worker](https://workers.cloudflare.com) + [Turso](https://turso.tech) + KV + cache API — FTS5 prefix search, edge-cached (60s), rate-limited
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

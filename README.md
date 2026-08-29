# typeahead

community actor search for [atproto](https://atproto.com). returns the full `profileViewBasic` surface minus `viewer` (`did`, `handle`, `displayName`, `avatar`, `associated`, `labels`, `createdAt`).

**live:** https://typeahead.waow.tech · machine-readable summary at [/llms.txt](https://typeahead.waow.tech/llms.txt)

canonical endpoint (self-owned [lexicon](lexicons/tech.waow.typeahead.searchActors.json)):

```bash
curl "https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=10"
```

also served at `/xrpc/app.bsky.actor.searchActorsTypeahead` as a drop-in alias for migrating off bluesky's appview — identical params and response, kept indefinitely.

profile cards for picked actors: `/xrpc/app.bsky.actor.getProfiles?actors=…` (≤25) and `/xrpc/app.bsky.actor.getProfile?actor=…` — displayName, description, avatar, banner, follower/follows/posts counts (bsky-sourced, omitted until enriched), labels, createdAt, indexedAt. no viewer state.

## stack

![typeahead architecture](docs/architecture-diagram.png)

- **edge worker**: [Cloudflare Worker](https://workers.cloudflare.com) in `src/`.
  It owns the public XRPC-compatible endpoint, 60s edge cache, admin ingest
  endpoint, Turso fallback, and the Bluesky rollback kill switch.
- **ingest service**: [Zig](https://ziglang.org) on [Fly.io](https://fly.io),
  `typeahead-ingester`. It streams profiles, posts, likes, and follows via
  the relay's firehose ([`com.atproto.sync.subscribeRepos`](https://atproto.com/specs/event-stream)), resolves identities, and
  batches actor updates to the worker.
- **source DB**: [Turso](https://turso.tech) / libSQL. It is the durable source
  of truth for actors, moderation overrides, and tombstones.
- **snapshot builder**: same Zig binary, `MODE=indexer`, run as a scheduled batch
  job on the home box (heavypad) via a Prefect deployment (`typeahead-index` in
  the `my-prefect-server` repo), **not** on Fly. It exports Turso, builds a
  precomputed prefix-index SQLite snapshot, and publishes `index.db` + a channel
  manifest to Cloudflare R2. Pure batch — no ingress, no SLA; a late/failed run
  just leaves the prior snapshot serving. (`fly.indexer.toml` is retained as a
  rollback path.)
- **search service**: same Zig binary, `typeahead-search`. It downloads and
  verifies snapshots from R2, attaches them read-only, tails Turso into a local
  actor replica, projects live changes into an overlay, and serves `/search`
  from bounded point lookups. Local FTS is disabled here.
- **identity**: three paths, in cost order. `getProfiles` (25 DIDs per call) in
  the hourly cron resolves most of them; [slingshot](https://microcosm.blue)
  resolves single DIDs from firehose `#identity` events; and a weekly job reads
  the PLC log in bulk for accounts the appview cannot see — self-hosted PDSes.
  See [docs/discovery-and-identity.md](docs/discovery-and-identity.md).

The serving invariant: no request does work proportional to the actor corpus.
The request path reads a small set of precomputed prefix keys, merges the live
overlay, hydrates a capped list of actors, and returns JSON. Bluesky is rollback
only, not part of the normal serving path.

## layout

- `src/` — Cloudflare Worker.
- `services/` — shared Zig service code. It builds three Fly roles from one
  codebase: ingest service (`fly.toml`), search service (`fly.search.toml`),
  and snapshot builder (`fly.indexer.toml`).
- `scripts/` — local/ops scripts for smoke tests, benchmarks, backfills, and
  maintenance.
- `docs/` — architecture notes, design docs, retros, and diagrams.

## dev

```bash
# worker
bun install && bunx wrangler dev

# Zig services
just test-services      # cd services && zig build test --summary all
cd services && zig build run
```

The one binary picks its role from the **required `MODE` env var** (no default —
boot fails without it), set per app in fly config:

- `MODE=ingester` (`fly.toml`): consume the relay's firehose, batch actor updates to the
  worker, run Turso schema migrations. Does NOT keep a local replica.
- `MODE=search` (`fly.search.toml`): sync Turso → local replica, promote/attach
  prefix-index snapshots, serve `/search` from snapshot + overlay.
- `MODE=indexer`: offline snapshot build, then exit. Runs on the home box
  (heavypad) as the scheduled `typeahead-index` Prefect deployment, every 3 days.
  `fly.indexer.toml` still describes the role and is the rollback path (launch a
  one-off Fly machine), but the home box is the primary builder.

The ingest service requires `TYPEAHEAD_URL` and `TYPEAHEAD_SECRET`. R2 and Turso
credentials are provided as Fly/Worker secrets in production.

## deploy

```bash
just deploy-worker    # edge worker to Cloudflare (manual; CI auto-deploy needs CLOUDFLARE_API_TOKEN set)
just deploy-ingest    # ingest service  (fly.toml)
just deploy-search    # search service  (fly.search.toml)
```

The snapshot builder runs on the home box (heavypad) as the `typeahead-index`
Prefect deployment (`my-prefect-server` repo), scheduled every 3 days — reads
Turso, builds, uploads to R2, exits. Host prereqs (zig + rclone) come from
`deploy/home-indexer/install.sh`. For a one-off / rollback build you can still
launch a Fly machine from `services/fly.indexer.toml` (`just
indexer-build-staging` for an isolated staging build). See `docs/operations.md`
and `docs/ephemeral-indexer-plan.md`.

# architecture

`typeahead.waow.tech` is an independent atproto actor typeahead service. It
serves the Bluesky-compatible
`tech.waow.typeahead.searchActors` route (and its `app.bsky.actor.searchActorsTypeahead`
compat alias) from our own index; Bluesky is a
rollback-only kill switch.

The current design has three flows:

1. **Continuous ingest** keeps Turso up to date from the relay firehose.
2. **Periodic offline builds** turn Turso into a precomputed SQLite prefix-index
   snapshot and publish it to R2.
3. **Bounded serving** answers requests from an attached snapshot plus a live
   overlay. No request scans the actor corpus.

See also:

- `docs/architecture-diagram.html` / `docs/architecture-diagram.svg`
- `docs/typeahead-index-design.md` for the longer design history
- `docs/index-serving-impl-plan.md` for the serving-path implementation plan

## components

### Edge worker

The Cloudflare Worker in `src/` is the public edge:

- exposes the typeahead endpoint: canonical `tech.waow.typeahead.searchActors` + bsky-compat alias (one handler, shared cache)
- caches search responses at the edge for 60 seconds
- accepts ingest batches on the admin path
- owns the Turso fallback path
- keeps the Bluesky emergency rollback switch available

Normal production traffic now flows through the search service, not Bluesky.

### Ingest service

`typeahead-ingester` runs the Zig service binary from `services/` in its
continuous ingest role. It reads the relay firehose, resolves identities, batches actor
updates, and POSTs them to the worker's admin ingest endpoint. The worker writes
the canonical rows to Turso.

The `services/` directory contains the shared Zig codebase for all Fly-side
roles: continuous ingest, search serving, and offline snapshot building.

### Source DB

Turso/libSQL is the durable source of truth:

- actors
- moderation overrides
- tombstones
- enrichment state

The search service treats Turso as the replication source for its local actor
table and live overlay. The snapshot builder treats Turso as the source for
offline index builds.

### Snapshot builder

The snapshot builder is the same Zig binary with `MODE=indexer`, launched as an
ephemeral Fly job from `services/fly.indexer.toml`.

It:

1. exports the Turso database
2. builds a SQLite snapshot with `prefix_top_base` and `actor_compact_base`
3. writes a manifest with size, sha256, versions, counts, and source watermark
4. uploads `index.db` and the configured channel manifest to Cloudflare R2
5. exits

The builder can use large temporary disk during construction, but the shipped
artifact is a standalone SQLite file. It is not served by an always-on indexer
machine.

### Search service

`typeahead-search` is the live read backend. It runs the same Zig binary with
firehose ingest disabled and sync/index promotion enabled.

It maintains:

- **attached snapshot**: read-only `index.db` from R2, attached under the index
  alias and swapped only after manifest, sha256, schema, version, and sentinel
  checks pass
- **live overlay**: `actor_overlay` and `prefix_overlay`, projected from recent
  Turso deltas so deletes, moderation changes, renames, new actors, and
  operator actions take effect without waiting for the next full build
- **actor sync replica**: local actors table used for hydration and the
  moderation safety belt

Local FTS maintenance is disabled on the search service. The request path uses
the precomputed prefix index instead.

## request path

For a query:

1. normalize the input
2. derive bounded lookup keys:
   - `e:` exact handle
   - `h:` handle prefix
   - `n:` display-name keys: Latin token prefixes plus CJK bigrams / leading
     unigram for Japanese/CJK typeahead
   - `d:` domain suffix / subdomain search
3. read matching DID lists from the attached snapshot
4. merge live overlay additions and suppressions
5. promote exact-handle matches
6. hydrate a capped candidate set from overlay first, then the base actor blobs
7. cross-check the local actor replica for hidden/tombstoned actors
8. return the Bluesky-compatible `profileViewBasic` shape

The important invariant is that every request performs bounded point lookups
and a small merge. It does not do FTS, `LIKE`, `ORDER BY`, or any operation
whose work grows with the total actor corpus.

## freshness

The base snapshot is periodic. Freshness between builds comes from the overlay.
The search service tails Turso deltas and projects them into prefix/actor
overlay tables:

- deletes and account removals suppress base results
- moderation hides suppress base results
- restores/show operations re-add actors
- handle/display-name changes remove old prefixes and add new prefixes
- newly indexed actors can appear before the next full snapshot

On promotion of a newer snapshot, overlay rows older than the snapshot
watermark can be compacted away while operator rows that must survive are kept.

This means staleness is self-correcting: the next snapshot refreshes the base,
and the overlay covers the gap.

## operational notes

- `services/fly.toml` deploys the continuous ingest service.
- `services/fly.search.toml` deploys the live search service.
- `services/fly.indexer.toml` is for one-off snapshot builder machines, not a
  daemon.
- The public rollback is to restore the `EMERGENCY_PROXY_BSKY` Worker secret.

# [typeahead](https://typeahead.waow.tech)

community-run actor search for [AT Protocol](https://atproto.com). search handles
and display names through an unauthenticated API backed by its own index.

[search](https://typeahead.waow.tech/) · [API docs](https://typeahead.waow.tech/docs) · [stats](https://typeahead.waow.tech/stats) · [llms.txt](https://typeahead.waow.tech/llms.txt)

## use

```sh
curl 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=10'
```

returns basic actor profiles without authenticated viewer state. the
[lexicon](lexicons/tech.waow.typeahead.searchActors.json) defines the response;
`app.bsky.actor.searchActorsTypeahead` is a compatible alias. an optional `pds`
parameter restricts results by indexed hosting PDS, including custom-domain handles.

profile cards are available through `app.bsky.actor.getProfile` and
`app.bsky.actor.getProfiles`. see the [API docs](https://typeahead.waow.tech/docs)
for parameters, moderation policy, caching and rate limits.

## design

- **continuous discovery** — a Zig ingester follows the relay's `subscribeRepos`
  firehose and writes actor updates to Turso. identity changes are resolved from
  DID authority and verified against the claimed handle.
- **local search** — compact snapshots in R2 supply the base index; a mutable
  overlay supplies changes between builds. HTTP serving stays separate from
  corpus-scale preparation.
- **edge serving** — a Cloudflare Worker handles public endpoints, caching,
  profile APIs and request accounting. a private transport routes search to Fly.
- **explicit policy** — moderation labels, domain blocks and per-DID overrides
  determine visibility independently of ranking. PDS membership comes from
  recorded hosting identity, rather than handle suffixes.
- **isolated refresh** — snapshots are prepared and qualified on an independent
  backend before conditional promotion. the previous backend remains available
  for rollback.

## develop

Worker development uses [Bun](https://bun.sh/) and [just](https://just.systems/).
the native services require Zig 0.17.0; offline tooling and retrieval checks use Python.
the Tantivy backend also requires Rust/Cargo and a C compiler.

```sh
bun install
bunx wrangler dev       # local Worker
just check             # types, lint, tests and deployment dry-runs
just test-services     # native unit tests
just test-search-recall # real builder/server retrieval regressions
just --no-dotenv test-tantivy # isolated engine, source and recovery checks
```

runtime credentials and role settings are required to run services. the native
binary has explicit ingester, search and indexer roles; a bare `zig build run`
does not configure one. repository workflows are in [AGENTS.md](AGENTS.md).

## deploy

```sh
just deploy-worker     # production Worker, followed by smoke checks
just deploy-ingest     # production ingester
```

backend refresh uses the [isolated refresh controller](docs/plans/automatic-snapshot-refresh.md),
not a direct deploy to the serving slot. Prefect deployment and schedule ownership
live in [my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server).
check smoke and benchmark targets before running them; some default to production.

## docs

[docs/](docs/) covers architecture, operations, engine evaluations and incident
history. deployment inventories, measurements and experiment results belong there.

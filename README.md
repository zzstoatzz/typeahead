# [typeahead](https://typeahead.waow.tech)

account search and profile lookup for [AT Protocol](https://atproto.com).
Typeahead helps people and apps find accounts across the network and retrieve
their public profiles. search by handle or display name through the website or
API—no sign-in required.

[search](https://typeahead.waow.tech/) · [API docs](https://typeahead.waow.tech/docs) · [stats](https://typeahead.waow.tech/stats) · [llms.txt](https://typeahead.waow.tech/llms.txt)

## use

```sh
curl 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=10'
```

returns basic actor profiles without authenticated viewer state. the
[lexicon](lexicons/tech.waow.typeahead.searchActors.json) defines the response;
`app.bsky.actor.searchActorsTypeahead` is a compatible alias. an optional `pds`
parameter restricts results to accounts indexed as belonging to a hosting server
(PDS), including accounts with custom-domain handles.

profile cards are available through `app.bsky.actor.getProfile` and
`app.bsky.actor.getProfiles`. see the [API docs](https://typeahead.waow.tech/docs)
for parameters, moderation policy, caching and rate limits.

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

See the [architecture and diagram](docs/architecture.md) for how search, live
updates and snapshot refresh fit together. [docs/](docs/) contains operating
guidance and evaluations; superseded designs live in [the archive](docs/archive/).

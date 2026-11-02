# Development

The public API and website run on a Cloudflare Worker. Install [Bun](https://bun.sh/)
and [just](https://just.systems/) to work on them:

```sh
bun install
bunx wrangler dev
just check
```

The ingester and the shared normalizer sources require Zig 0.17.0; offline
tooling uses Python. The Tantivy search backend also requires Rust/Cargo and a C
compiler. The ingester needs runtime credentials and an explicit `MODE=ingester`;
a bare `zig build run` does not configure one.

```sh
just test-services           # ingester and normalizer unit tests
just --no-dotenv test-tantivy # isolated engine, source and recovery checks
```

## Deployment

```sh
just deploy-worker     # production Worker, followed by smoke checks
just deploy-ingest     # production ingester
```

Backend refresh uses the [isolated refresh controller](plans/automatic-snapshot-refresh.md).
Prefect deployment and schedule ownership live in
[my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server).
Check smoke and benchmark targets before running them; some default to production.
See [operations](operations.md) and [repository workflows](../AGENTS.md).

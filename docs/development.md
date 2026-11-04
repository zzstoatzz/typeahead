# Development

The public API and website run on a Cloudflare Worker. Install [Bun](https://bun.sh/)
and [just](https://just.systems/) to work on them:

```sh
bun install
bunx wrangler dev
just check
```

For preview cards and PNG icons locally, run
`bunx wrangler dev -c deploy/image-renderer/wrangler.jsonc --port 8788`
in another terminal. Wrangler connects the public Worker's `IMAGE_RENDERER`
binding to this local renderer. The renderer needs no Turso or admin credentials.

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
just deploy-image-renderer # private renderer, followed by image smoke checks
just deploy-ingest     # production ingester
```

Deploy the image renderer before a public Worker release that introduces its
service binding. `just smoke-images` validates fresh PNGs and cache headers at
the public URLs. `just check` and CI build all three Workers.

Backend refresh uses the [isolated refresh controller](plans/automatic-snapshot-refresh.md).
Prefect deployment and schedule ownership live in
[my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server).
Check smoke and benchmark targets before running them; some default to production.
See [operations](operations.md) and [repository workflows](../AGENTS.md).

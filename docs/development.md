# Development

## The Workers

The public API and website, the search transport, the image renderer and the
eval collector are Cloudflare Workers in `src/`. Install [Bun](https://bun.sh/)
and [just](https://just.systems/):

```sh
bun install
bunx wrangler dev
just check
```

`just check` is what CI runs: type-check, lint, `bun test`, the route tool's
tests, the slot supervisor's tests, the smoke suite's own tests, and a dry-run
build of the four production Workers. It needs `python3`, `jq` and bash 5.2 or newer
(on macOS, `brew install bash`). It contacts nothing remote.

For preview cards and PNG icons locally, run the renderer in another terminal.
Wrangler connects the public Worker's `IMAGE_RENDERER` binding to it.

```sh
bunx wrangler dev -c deploy/image-renderer/wrangler.jsonc --port 8788
```

## Where Worker code lives

| path | what it holds | deployed by |
| --- | --- | --- |
| `src/index.ts`, `src/handlers/`, `src/pages/` | the public Worker: routes, the cron, endpoint handlers and HTML pages | `wrangler.jsonc` |
| `src/transport/` | the search transport: route record, route cache and Durable Object, validated search | `deploy/search-transport/` |
| `src/image-renderer/` | the PNG renderer, the only code that imports `workers-og` | `deploy/image-renderer/` |
| `src/eval/` | the eval collector and the report model the `/eval` page and `scripts/eval/` share | `deploy/eval/` |
| `src/stats/` | everything that reads or writes the `typeahead-stats` database | part of the public Worker |
| `src/control/` | everything that reads or writes the `typeahead-control` database | part of the public Worker |
| `src/experiments/` | the Worker placement probes | `deploy/placement-probe/` |

The rest of `src/` is flat on purpose: modules more than one of those use
(`types.ts`, `utils.ts`, `db.ts`, `slot-read.ts`, tracing and timing), and
moderation and identity policy. `moderation-regexes.json`,
`identity-quarantine.json` and `control-rule-vectors.json` are also read by the
Zig and Rust builds at those paths, so moving one is a slot image change.

Tests sit beside the module they test. `handlers/` and `pages/` are the only
directories named for a kind of file. A new directory is for a new Worker or
for a store with a single owning module.

## The slot's programs

A slot runs a store process written in Zig (`services/src/`) and a search
engine written in Rust on Tantivy (`services/tantivy/`). They need Zig 0.17.0,
Rust with Cargo, a C compiler and Python.

```sh
just test-services            # Zig unit tests: store, index bridge, control loop, normalizer
just --no-dotenv test-tantivy # the search engine against built binaries
just --no-dotenv test-bridge  # store to search engine, against a real local engine
just test-supervise           # the slot's supervisor against stand-in processes
just test-route         # the route tool against an in-memory bucket
```

Changes to matching or ranking also need `just --no-dotenv test-tantivy`,
which CI runs on main.

The store process is the binary in `services/`, started with `MODE=store`.
A bare `zig build run` sets no mode and exits. The slot image is
built from `deploy/slot/Dockerfile`:

```sh
docker build --platform linux/amd64 -f deploy/slot/Dockerfile -t typeahead-slot:local .
```

## Deploying

```sh
just deploy-worker            # public Worker, then smoke
just deploy-search-transport  # transport, then smoke
just deploy-image-renderer    # renderer, then image smoke
fly deploy . --config deploy/slot/fly.toml -a <standby app>
```

Push first. Deploys are run by hand; CI builds and tests only. A slot image
goes to the standby first and reaches the serving slot only after a flip:
[operating](operating.md#upgrade-the-slots) has the steps.

## Checking production

These read production and default to it. Check the target before running a
benchmark; do not load-test the serving slot.

```sh
just smoke          # response shape, ranking position, moderation, admin API
just index-status   # build, account count and versions on the serving slot
just bench          # edge latency beside Bluesky for the same queries
just bench-ranking  # ranking against Bluesky as the oracle
```

[Repository instructions](../AGENTS.md) say which of these a change needs.

Run `just eval-serve` and open `http://127.0.0.1:18790/eval` to inspect the
retained initial run. `just eval-tick` performs a daily collection only when
due, then prepares local snapshots. Viewing the charts sends no upstream
queries. [Evaluation](evaluation.md) covers score weights, retained history,
the isolated runner and the production import procedure.

## The pictures

`docs/diagrams/build.py` writes the five SVG files in `docs/diagrams/`. Edit
the script and run it; do not edit the SVG files.

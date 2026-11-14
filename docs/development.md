# Development

## The Workers

The public API and website, the search transport and the image renderer are
Cloudflare Workers in `src/`. Install [Bun](https://bun.sh/) and
[just](https://just.systems/):

```sh
bun install
bunx wrangler dev
just check
```

`just check` is what CI runs: type-check, lint, `bun test`, the route tool's
tests, the slot supervisor's tests, the smoke suite's own tests, and a dry-run
build of all three Workers. It needs `python3`, `jq` and bash 5.2 or newer
(on macOS, `brew install bash`). It contacts nothing remote.

For preview cards and PNG icons locally, run the renderer in another terminal.
Wrangler connects the public Worker's `IMAGE_RENDERER` binding to it.

```sh
bunx wrangler dev -c deploy/image-renderer/wrangler.jsonc --port 8788
```

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

The store process is the binary in `services/`, started with `MODE=store`
(`MODE=store-bootstrap` loads a new store). A bare `zig build run` sets no
mode and exits. The slot image is
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

## The pictures

`docs/diagrams/build.py` writes the five SVG files in `docs/diagrams/`. Edit
the script and run it; do not edit the SVG files.

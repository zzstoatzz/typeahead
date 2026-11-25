set dotenv-filename := ".dev.vars"

# NOTHING else checks types: wrangler bundles with esbuild and tests run under
# bun, and both strip types without looking at them. Every deploy recipe
# depends on this, or a type error ships silently.
[doc('type-check the Workers')]
[group('check')]
typecheck:
    bunx tsc --noEmit

# Worker unit tests
[group('check')]
test:
    bun test

# The anti-slop plugin is tools/oxlint/anti-slop, shared with plyr.fm and
# doodl. Zero findings is the bar; never disable a rule to get there.
[doc('oxlint with the anti-slop plugin')]
[group('check')]
lint:
    bunx oxlint --deny-warnings src scripts/eval

# Admission and conditional routing writes for a store-fed slot, against an
# in-memory bucket and fixture slots; no network.
[doc('the route tool and the statistics initializer')]
[group('check')]
test-route:
    python3 -W error -m unittest scripts/route/test_slot_route.py
    PYTHONPATH=scripts/route python3 -W error -m unittest scripts/route/test_initialize_statistics.py

# Restarts, backoff and the stop, against stand-in processes. Needs bash 5.2
# or newer (macOS: `brew install bash`) and jq; no network.
[doc("the slot's supervisor")]
[group('check')]
test-supervise:
    python3 -W error -m unittest deploy/slot/test_supervise.py

# The image's bash is older than Homebrew's and behaves differently. Needs
# Docker, and the network for apt.
[doc("the slot's supervisor under the slot image's bash")]
[group('check')]
test-supervise-image:
    docker run --rm -v "$PWD/deploy/slot:/src:ro" "$(sed -n 's/^FROM \(debian:[^ ]*\)$/\1/p' deploy/slot/Dockerfile)" sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 jq >/dev/null && bash --version | head -n 1 && cd / && python3 -W error -m unittest /src/test_supervise.py'

# What the suite counts as a failure, and that its override lifecycle never
# touches a real override; stand-in servers only.
[doc("the smoke suite's own logic")]
[group('check')]
test-smoke:
    python3 -W error -m unittest scripts.test_smoke_failures scripts.test_smoke_overrides

# Planning, the guards and the tag, against scratch repositories; no network.
[doc('the release script')]
[group('check')]
test-release:
    uv run --no-project python -W error -m unittest scripts.test_release

# what CI runs, locally
[group('check')]
check: typecheck lint test test-route test-supervise test-smoke test-release check-worker-builds

[group('check')]
check-worker-builds:
    node_modules/.bin/cf deploy --dry-run --no-provision
    node_modules/.bin/cf deploy --dry-run --no-provision --mode transport
    node_modules/.bin/cf deploy --dry-run --no-provision --mode renderer
    node_modules/.bin/cf deploy --dry-run --no-provision --mode collector

# zig unit tests for the store process and the shared normalizer sources
[group('check')]
test-services:
    cd services && zig build test --summary all

# Builds the engine from this checkout in a temporary tree and runs its
# receipts under both retrieval strategies. CI runs it on main.
[doc('the search engine: cargo tests, then every retrieval receipt')]
[group('check')]
test-tantivy:
    #!/usr/bin/env bash
    set -euo pipefail
    test "$(zig version)" = "0.17.0"
    ta_verify_root=$(mktemp -d "${TMPDIR:-/tmp}/typeahead-tantivy-check.XXXXXX")
    trap 'rm -rf "$ta_verify_root"' EXIT
    mkdir -p "$ta_verify_root/normalizer" "$ta_verify_root/tmp"
    export TMPDIR="$ta_verify_root/tmp"
    export PYTHONDONTWRITEBYTECODE=1
    export CARGO_BUILD_JOBS=2
    export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$PWD/services/tantivy/target}"
    export TA_NORMALIZER_LIB_DIR="$ta_verify_root/normalizer"
    export TA_IDENTITY_POLICY_PATH="$PWD/src/identity-quarantine.json"
    export TA_MODERATION_RULES_PATH="$PWD/src/moderation-regexes.json"
    unset CARGO_BUILD_TARGET TYPEAHEAD_TEST_CRASH TA_RETRIEVAL_STRATEGY
    python3 -c 'import hashlib,json,pathlib; p=pathlib.Path("src"); assert json.loads((p/"moderation-regexes.json").read_text())["canonical_source_sha256"] == hashlib.sha256((p/"moderation.ts").read_bytes()).hexdigest(), "canonical moderation artifact is stale"'
    python3 services/tantivy-normalizer/build.py --test
    python3 services/tantivy-normalizer/build.py --output "$TA_NORMALIZER_LIB_DIR/libtypeahead_normalizer.a"
    cargo test --release --locked --jobs 2 --manifest-path services/tantivy/Cargo.toml -- --test-threads=2
    cargo build --release --locked --jobs 2 --manifest-path services/tantivy/Cargo.toml
    cp "$CARGO_TARGET_DIR/release/typeahead-tantivy" "$ta_verify_root/typeahead-tantivy"
    export TA_BINARY="$ta_verify_root/typeahead-tantivy"
    export TANTIVY_TEST_BIN="$TA_BINARY"
    for strategy in per-segment global-merge; do
      export TA_RETRIEVAL_STRATEGY="$strategy"
      TA_SMOKE_RECEIPT="$ta_verify_root/smoke-$strategy.json" python3 services/tantivy/tests/smoke.py
      TA_BOOST_RECEIPT="$ta_verify_root/boost-$strategy.json" python3 services/tantivy/tests/boost_exact.py
    done
    export TA_RETRIEVAL_STRATEGY=global-merge
    TA_SOURCE_AUTHORITY_RECEIPT="$ta_verify_root/source-authority.json" python3 services/tantivy/tests/source_authority.py
    TA_SOURCE_POLICY_RECEIPT="$ta_verify_root/source-policy.json" python3 services/tantivy/tests/source_policy.py
    python3 services/tantivy/tests/source_version_only.py --binary "$TA_BINARY" --actors 128 --receipt "$ta_verify_root/source-version-only.json"
    TA_EXPIRY_COVERAGE_RECEIPT="$ta_verify_root/expiry.json" python3 services/tantivy/tests/expiry_coverage.py
    TA_SOURCE_PDS_RECEIPT="$ta_verify_root/source-pds.json" python3 services/tantivy/tests/source_pds.py
    TA_CONFLICT_DIAGNOSTICS_RECEIPT="$ta_verify_root/conflicts.json" python3 services/tantivy/tests/conflict_diagnostics.py
    TA_RESTART_MERGE_RECEIPT="$ta_verify_root/restart-merge.json" python3 services/tantivy/tests/restart_merge.py
    TA_MERGE_POLICY_RECEIPT="$ta_verify_root/merge-policy.json" python3 services/tantivy/tests/merge_policy.py
    TA_ACTOR_READ_RECEIPT="$ta_verify_root/actor-read.json" python3 services/tantivy/tests/actor_read.py
    python3 services/tantivy-normalizer/test_backend_adversarial.py

# Also runs the control loop's policy changes through a real store into its
# search. Loopback only, synthetic actors, everything in one temporary tree.
[doc('the index bridge against a Tantivy server built from this checkout')]
[group('check')]
test-bridge:
    #!/usr/bin/env bash
    set -euo pipefail
    test "$(zig version)" = "0.17.0"
    ta_bridge_root=$(mktemp -d "${TMPDIR:-/tmp}/typeahead-bridge-check.XXXXXX")
    trap 'rm -rf "$ta_bridge_root"' EXIT
    mkdir -p "$ta_bridge_root/normalizer"
    export CARGO_BUILD_JOBS=2
    export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$PWD/services/tantivy/target}"
    export TA_NORMALIZER_LIB_DIR="$ta_bridge_root/normalizer"
    export TA_IDENTITY_POLICY_PATH="$PWD/src/identity-quarantine.json"
    export TA_MODERATION_RULES_PATH="$PWD/src/moderation-regexes.json"
    unset CARGO_BUILD_TARGET TYPEAHEAD_TEST_CRASH
    python3 services/tantivy-normalizer/build.py --output "$TA_NORMALIZER_LIB_DIR/libtypeahead_normalizer.a"
    cargo build --release --locked --jobs 2 --manifest-path services/tantivy/Cargo.toml
    cp "$CARGO_TARGET_DIR/release/typeahead-tantivy" "$ta_bridge_root/typeahead-tantivy"
    cd services && TANTIVY_TEST_BIN="$ta_bridge_root/typeahead-tantivy" zig build test-bridge --summary all

# Fails while the binding is still the placeholder or the schema is not
# applied. Without the schema every admin policy change fails and no indexing
# request is logged. Reads the remote database through cf; writes nothing.
[doc('refuse to deploy a Worker whose control database is not ready for it')]
[group('deploy')]
control-ready:
    bun scripts/control-ready.ts --remote

# Fails on uncommitted changes to tracked files, or when HEAD is not
# origin/main. Each deploy labels its Worker version with the commit.
[doc('refuse to deploy anything but a clean checkout of origin/main')]
[group('deploy')]
published:
    #!/usr/bin/env bash
    set -euo pipefail
    git fetch --quiet origin main
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
      echo "uncommitted changes: commit and push before deploying" >&2; exit 1
    fi
    head=$(git rev-parse HEAD)
    if [ "$head" != "$(git rev-parse origin/main)" ]; then
      echo "HEAD is not origin/main: push, or check out origin/main, before deploying" >&2; exit 1
    fi
    if [ -n "${TYPEAHEAD_RELEASE_HEAD:-}" ] && [ "$head" != "$TYPEAHEAD_RELEASE_HEAD" ]; then
      echo "HEAD changed after the release was planned" >&2; exit 1
    fi

[private]
cf-deploy mode:
    #!/usr/bin/env bash
    set -euo pipefail
    head=$(git rev-parse HEAD)
    args=(deploy --no-provision --message "${head:0:12}")
    if [ "{{mode}}" != public ]; then args+=(--mode "{{mode}}"); fi
    if [ -n "${TYPEAHEAD_RELEASE_PROJECT:-}" ]; then
      if [ "$head" != "$TYPEAHEAD_RELEASE_HEAD" ]; then
        echo "HEAD changed after the release was planned" >&2; exit 1
      fi
      cd "$TYPEAHEAD_RELEASE_PROJECT"
      args+=(--prebuilt)
    fi
    exec node_modules/.bin/cf "${args[@]}"

# the public Worker, then the API and image smoke suites as the gate
[group('deploy')]
deploy-worker url=WORKER_URL: published control-ready lint typecheck
    just cf-deploy public
    @echo "--- waiting for edge propagation before smoke ---"
    sleep 5
    just smoke {{url}}
    just smoke-images {{url}}

# the private PNG renderer, then the image smoke suite
[group('deploy')]
deploy-image-renderer url=WORKER_URL: published lint typecheck
    just cf-deploy renderer
    just smoke-images {{url}}

# Deploy it before a public Worker that adds or changes its binding.
[doc('the search transport, then the API smoke suite')]
[group('deploy')]
deploy-search-transport url=WORKER_URL: published lint typecheck
    just cf-deploy transport
    just smoke {{url}}

# It has no HTTP surface, so nothing can smoke it; read its next cron tick.
[doc('the eval collector')]
[group('deploy')]
deploy-eval-collector: published lint typecheck
    just cf-deploy collector

# the public Worker WITHOUT the smoke gate (escape hatch)
[group('deploy')]
deploy-worker-only: published control-ready lint typecheck
    just cf-deploy public

# fresh public images through the renderer
[group('production')]
smoke-images url=WORKER_URL:
    uv run --no-project python scripts/smoke-images.py --url {{url}}

# A release tag (YYYY.MMDD.HHMMSS, UTC) names a commit of origin/main whose
# Workers are deployed and passed smoke. `--plan` prints what would deploy and
# stops; `--all` deploys every Worker; `--yes` does not ask. Slots are never
# deployed from here.
[doc('deploy the Workers that changed since the last release, then tag the commit')]
[group('deploy')]
release *ARGS:
    ./scripts/release.py {{ARGS}}

# commits on this checkout since the last release
[group('deploy')]
changelog:
    @git log --oneline "$(git tag --merged HEAD --list '[0-9][0-9][0-9][0-9].[0-9][0-9][0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]' | sort | tail -n 1)..HEAD"

# [Evaluation](docs/evaluation.md) says what each of these reads and writes.

# a finite manual collection run, subject to the persistent budget
[group('eval')]
eval-collect:
    bun scripts/eval/collect.ts

# the /eval page over retained runs, at http://127.0.0.1:18790/eval
[group('eval')]
eval-serve:
    bun scripts/eval/serve.ts

# validate and retain an existing run
[group('eval')]
eval-import path:
    bun scripts/eval/import.ts {{quote(path)}}

# prepare and verify local snapshots
[group('eval')]
eval-publish:
    bun scripts/eval/publish.ts

# exercise the importer against local Worker KV
[group('eval')]
eval-upload-local:
    bun scripts/eval/upload.ts --local

# run the importer against Cloudflare KV
[group('eval')]
eval-upload-remote:
    bun scripts/eval/upload.ts --remote

# one scheduled tick, with local snapshots
[group('eval')]
eval-tick:
    bun scripts/eval/schedule.ts

# one scheduled tick, with publication to Cloudflare KV
[group('eval')]
eval-tick-remote:
    bun scripts/eval/schedule.ts --remote

# These read production and default to it. AGENTS.md "performance &
# benchmarking" says what each layer measures and what good looks like.

WORKER_URL := "https://typeahead.waow.tech"
SEARCH_URL := "https://typeahead-search-transport.n8-3e9.workers.dev"

# correctness gate against the public API (shape, ranking, moderation, admin)
[group('production')]
smoke url=WORKER_URL:
    sh -c 'set -a; [ -f .env ] && . ./.env; set +a; ./scripts/smoke.py --url {{url}}'
alias prod-smoke := smoke

# edge latency, coverage, fields and stress against the public Worker (what users feel)
[group('production')]
bench *ARGS:
    ./scripts/bench.py {{ARGS}}

# Extra flags pass through; a trailing `--url X` overrides the default target.
[doc('backend latency through the active transport route, bypassing the edge cache')]
[group('production')]
bench-search *ARGS:
    ./scripts/bench.py --url {{SEARCH_URL}} --appb-direct --no-stress {{ARGS}}

# ranking quality against Bluesky as the oracle; non-zero exit on acceptance failure
[group('production')]
bench-ranking url=SEARCH_URL:
    ./scripts/bench.py --ranking --appb-direct --url {{url}}

# corpus scale on the serving slot: build_id, actor_count, normalize_version
[group('production')]
index-status url=SEARCH_URL:
    curl -sS {{url}}/index/status | jq .
alias search-status := index-status

# every layer in order: correctness, edge, engine, ranking, scale
[group('production')]
perf:
    just smoke
    just bench --quick
    just bench-search --quick
    just bench-ranking
    just index-status

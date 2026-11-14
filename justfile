set dotenv-filename := ".dev.vars"

# typecheck the worker. NOTHING else checks types: wrangler bundles with esbuild
# and tests run under bun, and both strip types without looking at them. Run this
# before deploying, or a type error ships silently.
typecheck:
    bunx tsc --noEmit

# worker unit tests
test:
    bun test

# oxlint with the anti-slop plugin (tools/oxlint/anti-slop, shared with plyr.fm
# and doodl). Zero findings is the bar; never disable a rule to get there.
lint:
    bunx oxlint --deny-warnings src


# admission and conditional routing writes for a store-fed slot, against an
# in-memory bucket and fixture slots; no network
test-route:
    python3 -W error -m unittest scripts/route/test_slot_route.py

# the shadow slot's supervisor against stand-in processes: restarts, backoff and
# the stop. Needs bash 5.2 or newer (macOS: `brew install bash`) and jq; no network
test-supervise:
    python3 -W error -m unittest deploy/slot/test_supervise.py

# the same tests under the bash the slot image has, which is older than
# Homebrew's and behaves differently. Needs Docker, and the network for apt
test-supervise-image:
    docker run --rm -v "$PWD/deploy/slot:/src:ro" "$(sed -n 's/^FROM \(debian:[^ ]*\)$/\1/p' deploy/slot/Dockerfile)" sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 jq >/dev/null && bash --version | head -n 1 && cd / && python3 -W error -m unittest /src/test_supervise.py'

# the smoke suite's own logic against stand-in servers: what it counts as a
# failure, and that its override lifecycle never touches a real override
test-smoke:
    python3 -W error -m unittest scripts.test_smoke_failures scripts.test_smoke_overrides

# what CI runs, locally
check: typecheck lint test test-route test-supervise test-smoke
    bunx wrangler deploy --dry-run
    bunx wrangler deploy -c deploy/search-transport/wrangler.jsonc --dry-run
    bunx wrangler deploy -c deploy/image-renderer/wrangler.jsonc --dry-run

# run the zig unit tests for the store process and the shared normalizer sources
test-services:
    cd services && zig build test --summary all

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

# run the index bridge against a real Tantivy server built from this checkout,
# and the control loop's policy changes through a real store into its search:
# loopback only, synthetic actors, everything in one temporary tree
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


# refuse to deploy a Worker whose control database is not ready for it: the
# binding is still the placeholder, or the schema is not applied. Without the
# schema every admin policy change fails and no indexing request is logged.
# Reads the remote database through wrangler; writes nothing
control-ready:
    @! grep -q '"database_id": "00000000-0000-0000-0000-000000000000"' wrangler.jsonc || { echo "wrangler.jsonc: CONTROL_DB has the placeholder database_id; put the typeahead-control database's id there" >&2; exit 1; }
    bun scripts/control-ready.ts --remote

# deploy the worker, then run the smoke suite as a post-deploy gate (fails loudly on regression)
deploy-worker url="https://typeahead.waow.tech": control-ready lint
    bunx tsc --noEmit
    npx wrangler deploy
    @echo "--- waiting for edge propagation before smoke ---"
    sleep 5
    just smoke {{url}}
    just smoke-images {{url}}

deploy-image-renderer url="https://typeahead.waow.tech": lint
    bunx tsc --noEmit
    bunx wrangler deploy -c deploy/image-renderer/wrangler.jsonc
    just smoke-images {{url}}

smoke-images url="https://typeahead.waow.tech":
    python3 scripts/smoke-images.py --url {{url}}

# Deploy the private origin transport before adding its binding to the public Worker.
deploy-search-transport url="https://typeahead.waow.tech": lint
    bunx tsc --noEmit
    bunx wrangler deploy -c deploy/search-transport/wrangler.jsonc
    just smoke {{url}}

# deploy the worker WITHOUT the post-deploy smoke gate (escape hatch)
deploy-worker-only: control-ready lint
    npx wrangler deploy

# ─── benchmarking & health ───────────────────────────────────────────────
# How to assess performance. Four layers, run from here. See AGENTS.md
# "Performance & benchmarking" for what each measures and what "good" looks like.
#
#   just smoke          correctness gate (shape, ranking, moderation, admin)
#   just bench          edge latency — what users feel (worker + CF cache)
#   just bench-search   backend latency — active transport route, no public edge cache
#   just bench-ranking  ranking quality vs bluesky oracle (acceptance-gated)
#   just index-status   corpus scale (actor_count, prefix_count, build_id)
#   just perf           run all of the above in sequence

WORKER_URL := "https://typeahead.waow.tech"
SEARCH_URL := "https://typeahead-search-transport.n8-3e9.workers.dev"

# correctness gate against the public API (shape, ranking, moderation, admin)
smoke url=WORKER_URL:
    sh -c 'set -a; [ -f .env ] && . ./.env; set +a; ./scripts/smoke.py --url {{url}}'
alias prod-smoke := smoke

# edge latency + coverage + fields + stress, against the prod worker (what users feel)
bench *ARGS:
    ./scripts/bench.py {{ARGS}}

# backend latency through the active transport route, bypassing the public edge cache.
# extra flags pass through; a trailing `--url X` overrides the default target.
bench-search *ARGS:
    ./scripts/bench.py --url {{SEARCH_URL}} --appb-direct --no-stress {{ARGS}}

# ranking quality vs bluesky oracle (+ /debug/search breakdowns); exits non-zero on acceptance failure
bench-ranking url=SEARCH_URL:
    ./scripts/bench.py --ranking --appb-direct --url {{url}}

# corpus scale: build_id, actor_count, prefix_count, normalize_version
index-status url=SEARCH_URL:
    curl -sS {{url}}/index/status | jq .
alias search-status := index-status

# full performance assessment: correctness → edge → engine → ranking → scale
perf:
    just smoke
    just bench --quick
    just bench-search --quick
    just bench-ranking
    just index-status

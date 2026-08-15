set dotenv-filename := ".dev.vars"

# typecheck the worker. NOTHING else checks types: wrangler bundles with esbuild
# and tests run under bun, and both strip types without looking at them. Run this
# before deploying, or a type error ships silently.
typecheck:
    bunx tsc --noEmit

# worker unit tests
test:
    bun test

# what CI runs, locally
check: typecheck test
    bunx wrangler deploy --dry-run

# run the zig unit tests for the services (ingester/search/indexer)
test-services:
    cd services && zig build test --summary all

# deploy the worker, then run the smoke suite as a post-deploy gate (fails loudly on regression)
deploy-worker url="https://typeahead.waow.tech":
    bunx tsc --noEmit
    npx wrangler deploy
    @echo "--- waiting for edge propagation before smoke ---"
    sleep 5
    just smoke {{url}}

# deploy the worker WITHOUT the post-deploy smoke gate (escape hatch)
deploy-worker-only:
    npx wrangler deploy

# deploy the ingest service (relay subscribeRepos -> worker admin ingest)
deploy-ingest:
    cd services && fly deploy -c fly.toml

# deploy the search service (snapshot + overlay serving). prod read path.
deploy-search:
    cd services && fly deploy -c fly.search.toml

# ─── benchmarking & health ───────────────────────────────────────────────
# How to assess performance. Four layers, run from here. See CLAUDE.md
# "Performance & benchmarking" for what each measures and what "good" looks like.
#
#   just smoke          correctness gate (shape, ranking, moderation, admin)
#   just bench          edge latency — what users feel (worker + CF cache)
#   just bench-search   engine latency — the query engine alone (search service, no cache)
#   just bench-ranking  ranking quality vs bluesky oracle (acceptance-gated)
#   just index-status   corpus scale (actor_count, prefix_count, build_id)
#   just perf           run all of the above in sequence

WORKER_URL := "https://typeahead.waow.tech"
SEARCH_URL := "https://typeahead-search.fly.dev"

# correctness gate against the public API (shape, ranking, moderation, admin)
smoke url=WORKER_URL:
    sh -c 'set -a; [ -f .env ] && . ./.env; set +a; ./scripts/smoke.py --url {{url}}'
alias prod-smoke := smoke

# edge latency + coverage + fields + stress, against the prod worker (what users feel)
bench *ARGS:
    ./scripts/bench.py {{ARGS}}

# engine latency — search service direct, bypassing the edge cache (stress off by default)
bench-search url=SEARCH_URL *ARGS:
    ./scripts/bench.py --url {{url}} --appb-direct --no-stress {{ARGS}}

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

# STAGING index build to an isolated R2 channel (never touches prod latest.json).
# branch namespaces the channel: staging/<branch>/ + latest.<branch>.json
indexer-build-staging branch="cjk-search":
    cd services && \
      INDEX_CHANNEL=staging \
      INDEX_STAGING_NAME="{{branch}}" \
      MODE=indexer zig build run

# dry-run or apply the prioritized enrichment walk (A: follows, B: indie PDS hop, C: followers).
# reads TURSO_URL/TURSO_AUTH_TOKEN from .env for the dedup query; --apply posts DID batches
# to /admin/enrich-profiles and should always be paired with --max-targets.
enrich-plan *ARGS:
    ./scripts/prioritized-enrich.py {{ARGS}}

# cleanup dead actors in batches with pauses between
cleanup batch_size="500" batches="10" pause="30":
    #!/usr/bin/env bash
    set -euo pipefail
    total_deleted=0
    total_updated=0
    total_checked=0
    last_rowid=0
    for i in $(seq 1 {{batches}}); do
        echo "=== batch $i/{{batches}} (size={{batch_size}}, starting at rowid $last_rowid) ==="
        output=$(./scripts/cleanup-dead-actors.py --limit {{batch_size}} --start-rowid "$last_rowid" 2>&1)
        echo "$output"
        # extract last rowid from output
        new_rowid=$(echo "$output" | grep -o 'rowid=[0-9]*' | tail -1 | cut -d= -f2)
        if [ -n "$new_rowid" ]; then
            last_rowid=$new_rowid
        fi
        # extract stats from final line
        last=$(echo "$output" | tail -1)
        d=$(echo "$last" | sed -n 's/.*deleted=\([0-9,]*\).*/\1/p' | tr -d ,)
        u=$(echo "$last" | sed -n 's/.*updated=\([0-9,]*\).*/\1/p' | tr -d ,)
        c=$(echo "$last" | sed -n 's/.*checked=\([0-9,]*\).*/\1/p' | tr -d ,)
        total_deleted=$((total_deleted + ${d:-0}))
        total_updated=$((total_updated + ${u:-0}))
        total_checked=$((total_checked + ${c:-0}))
        if [ "$i" -lt "{{batches}}" ]; then
            echo "--- pausing {{pause}}s before next batch ---"
            sleep {{pause}}
        fi
    done
    echo ""
    echo "=== done: checked=$total_checked deleted=$total_deleted updated=$total_updated ==="

# dry-run cleanup (no writes)
cleanup-dry batch_size="500":
    ./scripts/cleanup-dead-actors.py --dry-run --limit {{batch_size}}

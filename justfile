set dotenv-filename := ".dev.vars"

# run the zig unit tests for the services (ingester/search/indexer)
test-services:
    cd services && zig build test --summary all

# deploy the cloudflare worker (the edge / API). NOT the fly services.
deploy-worker:
    npx wrangler deploy

# deploy the ingest service (App A: jetstream -> worker)
deploy-ingest:
    cd services && fly deploy -c fly.toml

# deploy the search service (App B: snapshot+overlay serving). prod read path.
deploy-search:
    cd services && fly deploy -c fly.search.toml

# what snapshot is the search service on? (build_id, normalize_version, counts)
search-status url="https://typeahead-search.fly.dev":
    curl -sS {{url}}/index/status | jq .

# end-to-end smoke against the public API
prod-smoke url="https://typeahead.waow.tech":
    ./scripts/smoke.py --url {{url}}

# STAGING index build to an isolated R2 channel (never touches prod latest.json).
# branch namespaces the channel: staging/<branch>/ + latest.<branch>.json
indexer-build-staging branch="cjk-search":
    cd services && \
      INDEX_CHANNEL=staging \
      INDEX_STAGING_NAME="{{branch}}" \
      MODE=indexer zig build run

# perf benchmark against prod worker — latency, coverage, fields, stress
bench *ARGS:
    ./scripts/bench.py {{ARGS}}

# ranking-quality benchmark against the search service direct (with /debug/search breakdowns)
bench-ranking appb_url="https://typeahead-search.fly.dev":
    ./scripts/bench.py --ranking --appb-direct --url {{appb_url}}

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

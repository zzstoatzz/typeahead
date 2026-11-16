#!/usr/bin/env bash
set -euo pipefail

root=${1:?scratch root required}
expected_app=${2:?standby app required}
[[ ${FLY_APP_NAME:-} == "$expected_app" ]] || exit 64
cd "$root"
ulimit -f 32768
sampler=
scan=
cleanup() {
  trap - EXIT TERM INT HUP
  [[ -z $sampler ]] || kill "$sampler" 2>/dev/null || true
  [[ -z $scan ]] || kill "$scan" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP

guard() {
  curl -fsS --max-time 5 https://typeahead-search-transport.n8-3e9.workers.dev/route > route.json
  if grep -Eq '"origin"[[:space:]]*:[[:space:]]*"https://'"$expected_app"'\.fly\.dev"' route.json; then
    printf 'refusing to experiment on the serving slot\n' >&2
    return 1
  fi
}

sample() {
  trap - EXIT
  local stopping=0
  trap 'stopping=1' TERM INT HUP
  local phase=$1 i=0 encoded status elapsed engine result
  while IFS=$'\t' read -r case_id encoded; do
    printf '%s\t%s\n' "$case_id" "$encoded"
  done < queries.tsv > cases.tmp
  while true; do
    while IFS=$'\t' read -r case_id encoded; do
      ((stopping == 0)) || return 0
      local at
      at=$(date +%s%3N)
      result=$(curl -sS --max-time 4 --max-filesize 1048576 -D headers.tmp -o body.tmp -w '%{http_code} %{time_total}' "http://127.0.0.1:8080/debug/search?q=$encoded&limit=10") || true
      read -r status elapsed <<< "$result"
      status=${status:-0}
      [[ $status != 000 ]] || status=0
      engine=$(sed -nE 's/^[Ss]erver-[Tt]iming: search;dur=([0-9.]+).*/\1/p' headers.tmp | head -1)
      printf '{"phase":"%s","case":%s,"at_ms":%s,"http":%s,"elapsed_s":%s,"engine_ms":%s,"response":' "$phase" "$case_id" "$at" "$status" "${elapsed:-0}" "${engine:-null}" >> samples.jsonl
      if [[ $status == 200 && $(head -c 1 body.tmp) == '{' ]]; then
        cat body.tmp >> samples.jsonl
      else
        printf 'null' >> samples.jsonl
      fi
      printf '}\n' >> samples.jsonl
      i=$((i + 1))
      if ((i % 30 == 0)); then
        guard || { kill -TERM "$main_pid"; return 1; }
      fi
      ((stopping == 0)) || return 0
      sleep 1 || true
    done < cases.tmp
  done
}

phase() {
  local name=$1 rest=${2:-}
  guard
  printf '{"event":"phase_start","phase":"%s","at_ms":%s}\n' "$name" "$(date +%s%3N)" >> phases.jsonl
  sample "$name" &
  sampler=$!
  if [[ -z $rest ]]; then
    sleep 90
  else
    nice -n 10 env PROBE_STORE_DIR=/data/store PROBE_PAGE_REST_MS="$rest" ./corpus-scan-probe >> scan-results.jsonl 2>&1 &
    scan=$!
    wait "$scan"
    scan=
    printf '{"event":"scan_end","phase":"%s","at_ms":%s}\n' "$name" "$(date +%s%3N)" >> phases.jsonl
  fi
  kill "$sampler" 2>/dev/null || true
  wait "$sampler" 2>/dev/null || true
  sampler=
  printf '{"event":"phase_end","phase":"%s","at_ms":%s}\n' "$name" "$(date +%s%3N)" >> phases.jsonl
}

main_pid=$$
guard
phase quiet-before
phase unpaced-first 0
phase recovery-first
phase paced-20ms 20
phase recovery-second
phase unpaced-last 0
phase quiet-after
printf 'complete\n' > complete

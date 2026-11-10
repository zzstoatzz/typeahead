#!/usr/bin/env bash
# Seeds an empty shadow-slot volume from a published search bundle. Run it on
# the Fly machine while the supervisor idles (no bootstrap.json yet):
#
#   /app/bootstrap.sh [--index-only] [build_id]
#   /app/bootstrap.sh --store-only      # add the store to an index-only volume
#
# The store steps need RELAY_BOOTSTRAP_CURSOR; see load_store.
set -euo pipefail

data=${SHADOW_DATA:-/data}
scratch=$data/bootstrap
marker=$data/bootstrap.json
releases=releases/search-tantivy
reserve_bytes=$((1024 * 1024 * 1024))
replay_margin_s=${REPLAY_MARGIN_S:-3600}
tantivy=${TANTIVY_BIN:-/app/typeahead-tantivy}
ingester=${INGESTER_BIN:-/app/typeahead-ingester}
dump_actors=${DUMP_ACTORS_BIN:-/app/dump-actors}

index_only=false
store_only=false
build=""
for arg in "$@"; do
  case $arg in
    --index-only) index_only=true ;;
    --store-only) store_only=true ;;
    b[0-9]*) build=$arg ;;
    *) echo "usage: bootstrap.sh [--index-only | --store-only] [build_id]" >&2; exit 2 ;;
  esac
done

say() { printf '{"event":"bootstrap","step":"%s","detail":%s}\n' "$1" "${2:-null}"; }
die() { printf '{"event":"bootstrap_failed","reason":"%s"}\n' "$1" >&2; exit 1; }
free_bytes() { df --output=avail -B1 "$data" | tail -1 | tr -d ' '; }

preflight() {
  for name in INDEX_R2_ENDPOINT INDEX_R2_BUCKET INDEX_R2_ACCESS_KEY_ID INDEX_R2_SECRET_ACCESS_KEY; do
    [[ -n ${!name:-} ]] || die "$name is not set"
  done
  [[ -e $marker ]] && die "volume is already bootstrapped"
  [[ -e $data/tantivy || -e $data/store ]] && die "volume holds an index or a store without a receipt"
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_REGION=auto
  export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
  export RCLONE_CONFIG_R2_ENDPOINT=$INDEX_R2_ENDPOINT
  export RCLONE_CONFIG_R2_ACCESS_KEY_ID=$INDEX_R2_ACCESS_KEY_ID
  export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=$INDEX_R2_SECRET_ACCESS_KEY
  remote=${BOOTSTRAP_REMOTE:-r2:$INDEX_R2_BUCKET/$releases}
  mkdir -p "$scratch"
}

select_build() {
  [[ -n $build ]] && return
  build=$(rclone lsjson "$remote" -R --files-only --include '/*/manifest.json' --include '/*/index.tar' \
    | jq -r 'map({build: (.Path | split("/")[0]), name: .Name, at: .ModTime})
        | group_by(.build)
        | map(select(map(.name) | sort == ["index.tar", "manifest.json"]))
        | map({build: .[0].build, at: (map(select(.name == "manifest.json"))[0].at)})
        | max_by(.at).build // empty')
  [[ $build =~ ^b[0-9]{10}-[A-Za-z0-9_-]+$ ]] || die "no published bundle found"
}

read_manifest() {
  manifest=$scratch/manifest.json
  rclone cat "$remote/$build/manifest.json" --count 33554433 > "$manifest"
  (($(stat -c %s "$manifest") <= 33554432)) || die "manifest is larger than 32 MiB"
  jq -e --arg build "$build" --arg key "$releases/$build/index.tar" '
    .active_build == $build and .engine == "tantivy" and .object_key == $key
    and (.byte_size | type == "number") and (.sha256 | test("^[0-9a-f]{64}$"))
    and (.source_watermark | type == "number")
    and (.files | map(.path) | index("index/meta.json") != null)' "$manifest" >/dev/null \
    || die "manifest identity does not match $build"
  bundle_bytes=$(jq -r .byte_size "$manifest")
  bundle_sha256=$(jq -r .sha256 "$manifest")
  index_bytes=$(jq -r '[.files[] | select(.path | startswith("index/")) | .byte_size] | add' "$manifest")
  watermark=$(jq -r .source_watermark "$manifest")
  retrieval=$(jq -r .retrieval_strategy "$manifest")
  [[ $retrieval == "${INDEX_RETRIEVAL:-global-merge}" ]] || die "bundle retrieval strategy differs from INDEX_RETRIEVAL"
  say manifest "{\"build_id\":\"$build\",\"bundle_bytes\":$bundle_bytes,\"index_bytes\":$index_bytes,\"source_watermark\":$watermark}"
}

fetch_index() {
  (( $(free_bytes) >= bundle_bytes + index_bytes + reserve_bytes )) || die "not enough free disk for the bundle and its index"
  rclone copyto "$remote/$build/index.tar" "$scratch/index.tar" --multi-thread-streams 4 --multi-thread-cutoff 64M
  [[ $(stat -c %s "$scratch/index.tar") == "$bundle_bytes" ]] || die "bundle size differs from its manifest"
  echo "$bundle_sha256  $scratch/index.tar" | sha256sum --check --status || die "bundle checksum differs from its manifest"
  mkdir -p "$scratch/tantivy"
  tar -xf "$scratch/index.tar" -C "$scratch/tantivy" --wildcards 'index/*'
  rm "$scratch/index.tar"
  jq -r '.files[] | select(.path | startswith("index/")) | "\(.sha256)  \(.path)"' "$manifest" \
    | (cd "$scratch/tantivy" && sha256sum --check --status) || die "extracted index differs from its manifest"
  status=$("$tantivy" status --data "$scratch/tantivy" --writer-memory-mb 32) || die "reader rejected the extracted index"
  index_actors=$(jq -r .actor_count <<<"$status")
  say index "{\"actor_count\":$index_actors}"
}

# Loads the store from the index's own documents: dump-actors prints every
# stored actor as a JSON line, and the ingester's store-bootstrap mode commits
# them in bulk-load mode with progress lines. The relay, labeler and PLC
# cursors land in the loader's last commit, so a half-loaded store has none
# and the store process refuses to follow the relay on it. The load goes into
# store.loading and is renamed when it completes; an interrupted load is run
# again and continues over what is there.
#
# RELAY_BOOTSTRAP_CURSOR must be a relay seq older than the bundle; see
# "Cursors" in bootstrap.md. The loader does not yet check it against the
# relay's first frame. The PLC cursor is the watermark less the replay margin.
# LABELER_BOOTSTRAP_CURSOR is optional: without it the label stream starts at
# its head. BOOTSTRAP_POLICY names a JSON file of overrides and blocked
# domains to seed.
load_store() {
  local index_dir=$1
  [[ -x $dump_actors ]] || die "dump-actors is not at $dump_actors"
  [[ ${RELAY_BOOTSTRAP_CURSOR:-} =~ ^[0-9]+$ ]] || die "RELAY_BOOTSTRAP_CURSOR is not set"
  mkdir -p "$scratch"
  "$dump_actors" "$index_dir" >"$scratch/actors.jsonl" || die "could not read the index's actors"
  say dumped "{\"bytes\":$(stat -c %s "$scratch/actors.jsonl")}"
  local plc_cursor
  plc_cursor=$(date -u -d "@$((watermark - replay_margin_s))" +%Y-%m-%dT%H:%M:%S.000Z)
  env MODE=store-bootstrap STORE_DIR="$data/store.loading" BOOTSTRAP_ACTORS="$scratch/actors.jsonl" \
    BOOTSTRAP_RELAY_CURSOR="$RELAY_BOOTSTRAP_CURSOR" BOOTSTRAP_PLC_CURSOR="$plc_cursor" \
    ${LABELER_BOOTSTRAP_CURSOR:+BOOTSTRAP_LABELER_CURSOR="$LABELER_BOOTSTRAP_CURSOR"} \
    ${BOOTSTRAP_POLICY:+BOOTSTRAP_POLICY="$BOOTSTRAP_POLICY"} \
    "$ingester" || die "store load failed; run again to continue"
  mv "$data/store.loading" "$data/store"
  rm "$scratch/actors.jsonl"
  say store "{\"relay_cursor\":$RELAY_BOOTSTRAP_CURSOR,\"plc_cursor\":\"$plc_cursor\"}"
}

# Adds the store to a volume that was seeded with --index-only.
store_only() {
  [[ -e $marker ]] || die "volume has no bootstrap receipt; run without --store-only"
  [[ -e $data/store ]] && die "volume already holds a store"
  watermark=$(jq -r .source_watermark "$marker")
  load_store "$data/tantivy/index"
  jq '.store_loaded = true' "$marker" >"$marker.tmp"
  sync "$marker.tmp"
  mv "$marker.tmp" "$marker"
  rmdir "$scratch" 2>/dev/null || true
  say done "$(cat "$marker")"
}

finish() {
  mv "$scratch/tantivy" "$data/tantivy"
  jq -n --arg build "$build" --arg sha "$(sha256sum "$manifest" | cut -d' ' -f1)" \
    --argjson watermark "$watermark" --argjson margin "$replay_margin_s" \
    --argjson actors "$index_actors" --argjson store "$store_loaded" --arg retrieval "$retrieval" \
    '{schema_version: 1, build_id: $build, manifest_sha256: $sha, source_watermark: $watermark,
      replay_before: ($watermark - $margin), index_actor_count: $actors, store_loaded: $store,
      retrieval_strategy: $retrieval, completed_at: now | floor}' >"$marker.tmp"
  sync "$marker.tmp"
  mv "$marker.tmp" "$marker"
  rm -rf "$scratch"
  say done "$(cat "$marker")"
}

if $store_only; then
  store_only
  exit 0
fi
preflight
select_build
read_manifest
fetch_index
store_loaded=false
if ! $index_only; then
  load_store "$scratch/tantivy/index"
  store_loaded=true
fi
finish

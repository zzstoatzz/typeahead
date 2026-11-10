#!/usr/bin/env bash
# Seeds an empty shadow-slot volume from a published search bundle. Run it on
# the Fly machine while the supervisor idles (no bootstrap.json yet):
#
#   /app/bootstrap.sh [--index-only] [build_id]
#
# The index steps work today. The store steps are placeholders until the
# store process has a loader; without --index-only the script stops there
# and leaves the volume unmarked.
set -euo pipefail

data=${SHADOW_DATA:-/data}
scratch=$data/bootstrap
marker=$data/bootstrap.json
releases=releases/search-tantivy
reserve_bytes=$((1024 * 1024 * 1024))
replay_margin_s=${REPLAY_MARGIN_S:-3600}
tantivy=${TANTIVY_BIN:-/app/typeahead-tantivy}

index_only=false
build=""
for arg in "$@"; do
  case $arg in
    --index-only) index_only=true ;;
    b[0-9]*) build=$arg ;;
    *) echo "usage: bootstrap.sh [--index-only] [build_id]" >&2; exit 2 ;;
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

# Placeholder. The loader has to read every stored actor from
# $scratch/tantivy/index (experiments/zkv-scale/dump-actors does this), commit
# them to a new store at $data/store with backpressure against compaction,
# settle it, and report the number of actors committed.
load_store() {
  die "store loader is not built yet; rerun with --index-only to seed the index alone"
}

# Placeholder. Before the store process may follow the relay it needs cursors
# that are provably older than the bundle: see "Cursors" in bootstrap.md.
seed_cursors() {
  die "cursor seeding is not built yet"
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

preflight
select_build
read_manifest
fetch_index
store_loaded=false
if ! $index_only; then
  load_store
  seed_cursors
  store_loaded=true
fi
finish

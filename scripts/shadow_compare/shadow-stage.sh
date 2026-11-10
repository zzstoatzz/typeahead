#!/usr/bin/env bash
# Runs shadow_stage.py on the shadow machine, which has neither Python nor this
# repository. Everything it installs goes under /data/compare: uv, the Python
# uv downloads, and the comparison's sources from one commit of the public
# repository.
#
#   shadow-stage.sh <commit> run --run-id <id> --manifest-sha256 <sha256>
#   shadow-stage.sh <commit> clean --run-id <id>
#
# `run` reads COMPARE_R2_ENDPOINT, COMPARE_R2_BUCKET, COMPARE_R2_ACCESS_KEY_ID,
# COMPARE_R2_SECRET_ACCESS_KEY and COMPARE_R2_SESSION_TOKEN from the
# environment. Removing /data/compare removes everything this script made.
set -euo pipefail

root=${COMPARE_ROOT:-/data/compare}
repository=${COMPARE_REPOSITORY:-https://github.com/zzstoatzz/typeahead}
uv_version=0.13.4
uv_sha256=2b428396f12572d4429cf210ca234dd599a33b3af0958a4d5ed569d63e3f344f

die() { echo "error: $1" >&2; exit 2; }

commit=${1:-}
[[ $commit =~ ^[0-9a-f]{40}$ ]] || die "usage: shadow-stage.sh <full commit sha> run|clean ..."
shift
[[ $(uname -sm) == "Linux x86_64" ]] || die "this script installs uv for x86-64 Linux only"

mkdir -p "$root/bin"
if [[ ! -x $root/bin/uv ]]; then
  archive=$root/uv.tar.gz
  curl -fsSL "https://github.com/astral-sh/uv/releases/download/$uv_version/uv-x86_64-unknown-linux-gnu.tar.gz" -o "$archive"
  echo "$uv_sha256  $archive" | sha256sum --check --status || die "the uv archive differs from its pinned checksum"
  tar -xzf "$archive" -C "$root/bin" --strip-components=1
  rm "$archive"
fi

sources=$root/src/$commit
if [[ ! -e $sources/.complete ]]; then
  rm -rf "$sources"
  mkdir -p "$sources"
  curl -fsSL "$repository/archive/$commit.tar.gz" | tar -xz -C "$sources" --strip-components=1 --wildcards \
    '*/scripts/bench.py' '*/scripts/shadow_compare/*' '*/deploy/tantivy-dev-ui/candidate-smoke-cases.json' \
    || die "could not fetch $repository at $commit"
  touch "$sources/.complete"
fi

export UV_CACHE_DIR=$root/uv/cache UV_PYTHON_INSTALL_DIR=$root/uv/python
export UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1 UV_NO_PROGRESS=1 PYTHONDONTWRITEBYTECODE=1
exec "$root/bin/uv" run --script --quiet "$sources/scripts/shadow_compare/shadow_stage.py" --root "$root" "$@"

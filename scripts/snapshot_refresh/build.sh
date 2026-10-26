#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT HUP INT TERM
cd "$root/services"
zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
cp zig-out/bin/typeahead-ingester zig-out/bin/admit-prefix "$context/"
cp "$root/scripts/snapshot_refresh/install.py" "$context/"
docker build --platform linux/amd64 -f "$root/scripts/snapshot_refresh/Dockerfile" --build-arg "SOURCE_REVISION=$(git rev-parse HEAD)" -t "$1" "$context"
docker push "$1"

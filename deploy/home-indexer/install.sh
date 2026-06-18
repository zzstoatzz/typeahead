#!/usr/bin/env bash
# host prereqs for running the typeahead snapshot builder (MODE=indexer) on the
# home box (heavypad) as a Prefect home-pool batch job. Installs the same Zig +
# rclone versions the Fly Dockerfile pins, to ~/.local/bin (on the worker PATH).
# Idempotent: re-running upgrades/repins in place.
set -euo pipefail

ZIG_VERSION=0.16.0
RCLONE_VERSION=1.69.1
ARCH=x86_64
BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt"
mkdir -p "$BIN" "$OPT"

# --- zig 0.16.0 (matches services/Dockerfile) ---
if [ "$("$BIN/zig" version 2>/dev/null || true)" != "$ZIG_VERSION" ]; then
  echo "installing zig $ZIG_VERSION ..."
  tmp=$(mktemp -d)
  curl -fL "https://ziglang.org/download/${ZIG_VERSION}/zig-${ARCH}-linux-${ZIG_VERSION}.tar.xz" \
    | tar -xJ -C "$tmp"
  rm -rf "$OPT/zig-${ARCH}-linux-${ZIG_VERSION}"
  mv "$tmp/zig-${ARCH}-linux-${ZIG_VERSION}" "$OPT/"
  ln -sf "$OPT/zig-${ARCH}-linux-${ZIG_VERSION}/zig" "$BIN/zig"
  rm -rf "$tmp"
else
  echo "zig $ZIG_VERSION already present"
fi

# --- rclone 1.69.1 (matches services/Dockerfile; R2 multipart transport) ---
if [ "$("$BIN/rclone" version 2>/dev/null | head -1 | awk '{print $2}' || true)" != "v${RCLONE_VERSION}" ]; then
  echo "installing rclone $RCLONE_VERSION ..."
  tmp=$(mktemp -d)
  curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" -o "$tmp/rclone.zip"
  unzip -oj "$tmp/rclone.zip" 'rclone-*/rclone' -d "$BIN"
  chmod +x "$BIN/rclone"
  rm -rf "$tmp"
else
  echo "rclone $RCLONE_VERSION already present"
fi

echo "--- versions ---"
"$BIN/zig" version
"$BIN/rclone" version | head -1
echo "done. ensure $BIN is on the worker PATH (it already is for the home-pool unit)."

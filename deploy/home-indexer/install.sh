#!/usr/bin/env bash
# host prereqs for the typeahead batch jobs that run on the home box (heavypad)
# as Prefect home-pool deployments:
#   - typeahead-index         (MODE=indexer snapshot builder) -> zig, rclone
#   - typeahead-plc-identity  (bulk DID identity from the PLC log) -> allegedly
# Installs the same Zig + rclone versions the Fly Dockerfile pins, to
# ~/.local/bin (on the worker PATH). Idempotent: re-running upgrades in place.
set -euo pipefail

ZIG_VERSION=0.17.0
RCLONE_VERSION=1.69.1
ARCH=x86_64
BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt"
mkdir -p "$BIN" "$OPT"

# --- zig 0.17.0 (matches services/Dockerfile and build.zig.zon) ---
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

# --- allegedly (microcosm PLC tooling; scrapes the weekly bundles the public
# host has not published yet) ---
# Optional but load-bearing for recent accounts: plc.t3.storage.dev publishes
# bundles only through 2025-09-18, and the unpublished tail is precisely where
# newly-created accounts live. Without this, typeahead-plc-identity still runs
# but cannot resolve anything created since that date, and says so in its logs.
if ! command -v allegedly >/dev/null 2>&1; then
  if command -v cargo >/dev/null 2>&1; then
    echo "installing allegedly ..."
    cargo install allegedly
  else
    echo "WARNING: cargo not found — skipping allegedly."
    echo "  typeahead-plc-identity will run against published bundles only,"
    echo "  leaving accounts created after 2025-09-18 unresolved."
    echo "  install rust (https://rustup.rs) then re-run this script."
  fi
else
  echo "allegedly already present"
fi

echo "--- versions ---"
"$BIN/zig" version
"$BIN/rclone" version | head -1
command -v allegedly >/dev/null 2>&1 && allegedly --version || echo "allegedly: not installed"
echo "done. ensure $BIN is on the worker PATH (it already is for the home-pool unit)."

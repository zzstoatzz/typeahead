#!/usr/bin/env sh
# Install the pinned Bun into $1 (default .bun).
#
# The CI image's Bun is 1.3.13. On Linux, 1.3.13 and 1.3.14 fail this suite
# intermittently when it starts workerd (`EBADF: bad file descriptor,
# epoll_ctl` from child_process.spawn, after which `bun test` can hang);
# 1.4.0 does not. The sha256 is from the release's SHASUMS256.txt.
#
# The binary is a generic Linux build: on NixOS, run scripts/ci-workerd.sh on
# it afterwards to point it at a loader.
#
# To move versions: change BUN_VERSION and BUN_SHA256 together.
set -eu

BUN_VERSION="${BUN_VERSION:-1.4.0}"
BUN_TARGET="${BUN_TARGET:-linux-x64}"
BUN_SHA256="${BUN_SHA256:-2d03fb5fb83ac8b567aca0a281b2ce1a1a19d488f56c2968d88c3f25e92fe452}"

dest="${1:-.bun}"
archive="$dest.zip"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

curl -fsSL --retry 2 --max-time 300 \
  "https://github.com/oven-sh/bun/releases/download/bun-v$BUN_VERSION/bun-$BUN_TARGET.zip" -o "$archive"
if [ "$(sha256 "$archive")" != "$BUN_SHA256" ]; then
  rm -f "$archive"
  echo "bun checksum mismatch" >&2
  exit 1
fi

rm -rf "$dest"
mkdir -p "$dest"
unzip -q -j "$archive" "bun-$BUN_TARGET/bun" -d "$dest"
rm "$archive"
chmod +x "$dest/bun"
ln -sf bun "$dest/bunx"

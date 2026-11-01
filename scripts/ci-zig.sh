#!/usr/bin/env sh
# Install the pinned Zig compiler into $1 (default .zig).
#
# ziglang.org prunes dev builds, so fall back through the community mirrors
# (https://ziglang.org/download/community-mirrors.txt). The pinned sha256 comes
# from ziglang.org's index.json, so a mirror cannot substitute other bytes.
#
# To move compilers: change ZIG_VERSION and ZIG_SHA256 together.
set -eu

ZIG_VERSION="${ZIG_VERSION:-0.17.0}"
ZIG_TARGET="${ZIG_TARGET:-x86_64-linux}"
ZIG_SHA256="${ZIG_SHA256:-1cbe9df9f27e6b78d14ccbca43b6703a404ef79ef1c463de901d7f088d4e2026}"
ZIG_SOURCES="${ZIG_SOURCES:-https://ziglang.org/builds https://pkg.hexops.org/zig https://zigmirror.com https://zig.squirl.dev}"

dest="${1:-.zig}"
name="zig-$ZIG_TARGET-$ZIG_VERSION"
archive="$dest.tar.xz"

if [ -x "$dest/zig" ] && [ "$("$dest/zig" version)" = "$ZIG_VERSION" ]; then
  exit 0
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

ok=0
for base in $ZIG_SOURCES; do
  if ! curl -fsSL --retry 2 --max-time 300 "$base/$name.tar.xz" -o "$archive"; then
    echo "zig download failed from $base" >&2
    continue
  fi
  if [ "$(sha256 "$archive")" != "$ZIG_SHA256" ]; then
    echo "zig checksum mismatch from $base" >&2
    continue
  fi
  echo "zig $ZIG_VERSION from $base"
  ok=1
  break
done
if [ "$ok" != 1 ]; then
  rm -f "$archive"
  echo "no source served a valid $name.tar.xz" >&2
  exit 1
fi

rm -rf "$dest"
mkdir -p "$dest"
tar -xJf "$archive" -C "$dest" --strip-components=1
rm "$archive"
test "$("$dest/zig" version)" = "$ZIG_VERSION"

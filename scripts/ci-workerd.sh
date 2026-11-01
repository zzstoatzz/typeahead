#!/usr/bin/env sh
# Make npm's prebuilt workerd runnable on CI's NixOS image.
#
# The binary asks for /lib64/ld-linux-x86-64.so.2 and needs only glibc
# (libc, libm), but NixOS has no /lib64 loader, so Miniflare cannot start it.
# Point the binary at the glibc that the gcc wrapper is built against.
set -eu

bin="${1:-node_modules/@cloudflare/workerd-linux-64/bin/workerd}"
loader="$(cat "$(dirname "$(readlink -f "$(command -v gcc)")")/../nix-support/dynamic-linker")"

patchelf --set-interpreter "$loader" --set-rpath "$(dirname "$loader")" "$bin"
"$bin" --version

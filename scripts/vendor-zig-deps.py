#!/usr/bin/env python3
"""vendor the services/ zig dependency closure into services/.zig-pkg-vendor.

The Dockerfile seeds /root/.cache/zig/p from that directory, so builds
survive tangled.org 503-ing every fetch (2026-08-14: both the fly depot
builder and local docker were blocked while plain curl succeeded; zig's
fetch client appears to trip tangled's edge protection).

Walks build.zig.zon hashes transitively, copying each package's hash-named
tarball (or extracted dir) from the host zig cache (~/.cache/zig/p).
Run after bumping any dep in services/build.zig.zon, then rebuild.
Missing hashes are reported; lazy deps never fetched locally are fine to skip.
"""

import os
import re
import shutil
import sys
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(REPO, "services")
CACHE = os.path.expanduser("~/.cache/zig/p")
DEST = os.path.join(SERVICES, ".zig-pkg-vendor")


def zon_hashes(text: str) -> list[str]:
    return re.findall(r'\.hash\s*=\s*"([^"]+)"', text)


def read_zon_from_pkg(h: str) -> str:
    d = os.path.join(CACHE, h)
    if os.path.isdir(d):
        p = os.path.join(d, "build.zig.zon")
        return open(p).read() if os.path.exists(p) else ""
    t = os.path.join(CACHE, h + ".tar.gz")
    if os.path.exists(t):
        with tarfile.open(t) as tf:
            for m in tf.getmembers():
                if m.name.endswith("build.zig.zon") and m.name.count("/") <= 1:
                    return tf.extractfile(m).read().decode()
    return ""


def main() -> int:
    os.makedirs(DEST, exist_ok=True)
    seen: set[str] = set()
    todo = zon_hashes(open(os.path.join(SERVICES, "build.zig.zon")).read())
    missing: list[str] = []
    while todo:
        h = todo.pop()
        if h in seen:
            continue
        seen.add(h)
        copied = False
        for s in (os.path.join(CACHE, h + ".tar.gz"), os.path.join(CACHE, h)):
            if os.path.exists(s):
                d = os.path.join(DEST, os.path.basename(s))
                if not os.path.exists(d):
                    (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, d)
                copied = True
                break
        if not copied:
            missing.append(h)
        todo.extend(zon_hashes(read_zon_from_pkg(h)))
    n = len([f for f in os.listdir(DEST) if f != ".keep"])
    print(f"vendored {n} packages into {DEST}")
    if missing:
        print("not in host cache (lazy deps are fine to skip):")
        for h in missing:
            print(f"  {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import hashlib
import json
import mmap
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

from verify import iter_new

MAGIC = b"FLATPX05"
HEADER = struct.Struct("<8sIIIIQQQQQQQ")
PAGE = 8192
ALIGN = 4096
MAX_EXTENT = 16 * 1024 * 1024


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def source_rows(connection):
    for did, blob in connection.execute(
        "SELECT did,blob FROM actor_compact_base ORDER BY did"
    ):
        yield b"\1" + did.encode("utf8"), blob
    for key, blob in connection.execute(
        "SELECT key,dids FROM prefix_top_base ORDER BY key"
    ):
        yield b"\2" + key.encode("utf8"), blob


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def build(source, output_dir):
    source = Path(source).resolve()
    output_dir = Path(output_dir).absolute()
    start = time.monotonic()
    if output_dir.exists():
        raise FileExistsError(output_dir)
    if not output_dir.parent.is_dir():
        raise FileNotFoundError(output_dir.parent)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        if src.execute(
            "SELECT type FROM sqlite_master WHERE name='prefix_top_base'"
        ).fetchone() != ("table",):
            raise ValueError("source requires legacy prefix table")
        if src.execute(
            "SELECT 1 FROM index_meta WHERE key IN ('posting_format','prefix_storage_format','dense_shared_version','flat_prefix_version') LIMIT 1"
        ).fetchone():
            raise ValueError("source must be legacy snapshot")
        with tempfile.TemporaryDirectory(
            dir=output_dir.parent, prefix=".px05-build-"
        ) as temp:
            work = Path(temp)
            staging = work / "snapshot"
            staging.mkdir()
            side = staging / "index.db.prefix"
            database = staging / "index.db"
            roots = []
            entries = bytearray()
            patches = []
            restarts = []
            seen = {}
            previous = b""
            count = 0
            postingbytes = 0
            first = None
            keys = oversized = 0
            with (work / "pages").open("wb") as pages:

                def flush():
                    nonlocal entries, patches, restarts, seen, previous
                    nonlocal count, postingbytes, first, oversized
                    if not count:
                        return
                    keystart = 16 + 2 * len(restarts)
                    keyend = keystart + len(entries)
                    used = keyend + postingbytes
                    extent = (
                        PAGE if used <= PAGE else (used + ALIGN - 1) // ALIGN * ALIGN
                    )
                    if extent > MAX_EXTENT or (extent > PAGE and count != 1):
                        raise ValueError("extent bound")
                    postings = bytearray()
                    offsets = {}
                    for blob in seen:
                        where = keyend + len(postings)
                        if where > 65535:
                            raise ValueError("posting start exceeds u16")
                        offsets[blob] = where
                        postings.extend(struct.pack("<I", len(blob)))
                        postings.extend(blob)
                    for position, blob in patches:
                        struct.pack_into("<H", entries, position, offsets[blob])
                    restart_offsets = [keystart + offset for offset in restarts]
                    if max(restart_offsets) > 65535:
                        raise ValueError("restart exceeds u16")
                    roots.append((first, pages.tell()))
                    pages.write(struct.pack("<IIII", count, keyend, used, 0))
                    pages.write(
                        struct.pack("<" + "H" * len(restarts), *restart_offsets)
                    )
                    pages.write(entries)
                    pages.write(postings)
                    pages.write(bytes(extent - used))
                    oversized += extent > PAGE
                    entries = bytearray()
                    patches = []
                    restarts = []
                    seen = {}
                    previous = b""
                    count = 0
                    postingbytes = 0
                    first = None

                for key, blob in source_rows(src):
                    if (
                        len(key) > 8193
                        or not isinstance(blob, bytes)
                        or len(blob) > 0xFFFFFFFF
                    ):
                        raise ValueError("source lengths")
                    while True:
                        shared = 0
                        if count % 16:
                            while (
                                shared < min(len(key), len(previous))
                                and key[shared] == previous[shared]
                            ):
                                shared += 1
                        prefix = varint(shared) + varint(len(key) - shared)
                        entrysize = len(prefix) + 2 + len(key) - shared
                        is_new = blob not in seen
                        post = 4 + len(blob) if is_new else 0
                        used = (
                            16
                            + 2 * ((count + 16) // 16)
                            + len(entries)
                            + entrysize
                            + postingbytes
                            + post
                        )
                        if count and used > PAGE:
                            flush()
                            continue
                        if (used + ALIGN - 1) // ALIGN * ALIGN > MAX_EXTENT:
                            raise ValueError("oversized singleton exceeds16MiB")
                        if not is_new:
                            blob = seen[blob]
                        if first is None:
                            first = key
                        if count % 16 == 0:
                            restarts.append(len(entries))
                        entries.extend(prefix)
                        patches.append((len(entries), blob))
                        entries.extend(b"\0\0")
                        entries.extend(key[shared:])
                        seen[blob] = blob
                        postingbytes += post
                        previous = key
                        count += 1
                        keys += 1
                        if used > PAGE:
                            flush()
                        break
                    if keys % 10000000 == 0:
                        print(
                            json.dumps(
                                {
                                    "phase": "pack",
                                    "keys": keys,
                                    "seconds": time.monotonic() - start,
                                }
                            ),
                            file=sys.stderr,
                            flush=True,
                        )
                flush()
            records = [
                struct.pack("<H", len(key)) + key + struct.pack("<Q", offset)
                for key, offset in roots
            ]
            offsets = [0]
            for record in records:
                offsets.append(offsets[-1] + len(record))
            if offsets[-1] > 0xFFFFFFFF:
                raise ValueError("root directory exceeds4GiB")
            rs = 80 + 4 * len(offsets)
            pd = (rs + offsets[-1] + ALIGN - 1) // ALIGN * ALIGN
            size = pd + (work / "pages").stat().st_size
            with side.open("xb") as out:
                out.write(
                    HEADER.pack(MAGIC, 5, len(roots), 0, 0, rs, pd, size, 0, 0, 0, 0)
                )
                out.write(struct.pack("<" + "I" * len(offsets), *offsets))
                out.write(b"".join(records))
                out.write(bytes(pd - out.tell()))
                with (work / "pages").open("rb") as pages:
                    shutil.copyfileobj(pages, out)
            (work / "pages").unlink()
            with (
                side.open("rb") as f,
                mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data,
            ):
                verified = 0
                for (key, blob), (newkey, newblob) in zip(
                    source_rows(src),
                    iter_new(data),
                    strict=True,
                ):
                    if key != newkey or blob != newblob:
                        raise AssertionError("exhaustive key/value mismatch")
                    verified += 1
                side_digest = hashlib.sha256(data).hexdigest()
            dst = sqlite3.connect(database)
            try:
                src.backup(dst)
                dst.executescript(
                    "DROP TABLE prefix_top_base;CREATE VIEW prefix_top_base AS SELECT CAST(NULL AS TEXT) AS key,CAST(NULL AS BLOB) AS dids WHERE 0;"
                    "DROP TABLE actor_compact_base;CREATE VIEW actor_compact_base AS SELECT CAST(NULL AS TEXT) AS did,CAST(NULL AS BLOB) AS blob WHERE 0;"
                )
                dst.executemany(
                    "INSERT OR REPLACE INTO index_meta(key,value) VALUES(?,?)",
                    [("flat_prefix_version", "5"), ("flat_prefix_sha256", side_digest)],
                )
                dst.commit()
                dst.execute("VACUUM")
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AssertionError("SQLite integrity")
            finally:
                dst.close()
            manifest = {
                "format": "FLATPX05",
                "keys": keys,
                "verified_keys": verified,
                "pages": len(roots),
                "oversized_pages": oversized,
                "sqlite_bytes": database.stat().st_size,
                "prefix_bytes": side.stat().st_size,
                "total_bytes": database.stat().st_size + side.stat().st_size,
                "sha256": {
                    "index.db": digest_file(database),
                    "index.db.prefix": side_digest,
                },
                "source": str(source),
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
            for file in staging.iterdir():
                with file.open("rb") as f:
                    os.fsync(f.fileno())
            sync_directory(staging)
            if output_dir.exists():
                raise FileExistsError(output_dir)
            os.rename(staging, output_dir)
            sync_directory(output_dir.parent)
            return manifest
    finally:
        src.close()


if __name__ == "__main__":
    print(json.dumps(build(*sys.argv[1:]), indent=2))

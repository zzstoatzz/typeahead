import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from native import pack, verify


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
            stats = pack(source, side, work / "pages")
            (work / "pages").unlink()
            side_digest = digest_file(side)
            verify(source, side, side_digest, stats)
            keys = stats.keys
            verified = stats.verified_keys
            pages = stats.pages
            oversized = stats.oversized_pages
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
                "pages": pages,
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

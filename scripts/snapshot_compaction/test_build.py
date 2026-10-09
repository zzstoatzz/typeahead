import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from build import build
from verify import iter_new


def source(path, actors, prefixes):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY, blob BLOB NOT NULL) WITHOUT ROWID;
        CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY, dids BLOB NOT NULL) WITHOUT ROWID;
        CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE preserved_pds(did TEXT PRIMARY KEY, host TEXT);
        INSERT INTO index_meta VALUES('build_id','original');
        INSERT INTO preserved_pds VALUES('did:arbitrary','example.test');
    """)
    db.executemany("INSERT INTO actor_compact_base VALUES(?,?)", actors)
    db.executemany("INSERT INTO prefix_top_base VALUES(?,?)", prefixes)
    db.commit()
    db.close()


class Tests(unittest.TestCase):
    def test_namespaces_exact_opaque_values_and_metadata(self):
        for count in (0, 1, 17, 2500):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                actors = [
                    (f"key{i:08d}", b"\x00\xffopaque" + bytes([i % 256]))
                    for i in range(count)
                ]
                prefixes = [
                    (f"key{i:08d}", b"not-posting" + bytes([i % 19]))
                    for i in range(count)
                ]
                if count == 2500:
                    actors += [("東京", b""), ("x" * 8192, b"a" * 80000)]
                    prefixes += [("東京", b"\x02"), ("x" * 8192, b"a" * 80000)]
                source(root / "source.db", actors, prefixes)
                before = (root / "source.db").read_bytes()
                manifest = build(root / "source.db", root / "output")
                data = (root / "output/index.db.prefix").read_bytes()
                expected = sorted(
                    [(b"\1" + k.encode(), v) for k, v in actors]
                    + [(b"\2" + k.encode(), v) for k, v in prefixes]
                )
                self.assertEqual(list(iter_new(data)), expected)
                self.assertEqual(manifest["verified_keys"], len(expected))
                self.assertEqual((root / "source.db").read_bytes(), before)
                self.assertEqual(
                    manifest["sha256"]["index.db.prefix"],
                    hashlib.sha256(data).hexdigest(),
                )
                db = sqlite3.connect(root / "output/index.db")
                self.assertEqual(
                    db.execute("SELECT * FROM actor_compact_base").fetchall(), []
                )
                self.assertEqual(
                    db.execute("SELECT * FROM prefix_top_base").fetchall(), []
                )
                self.assertEqual(
                    db.execute("SELECT * FROM preserved_pds").fetchall(),
                    [("did:arbitrary", "example.test")],
                )
                self.assertEqual(
                    dict(db.execute("SELECT * FROM index_meta"))["flat_prefix_version"],
                    "5",
                )
                db.close()
                self.assertFalse(list(root.glob(".px05-build-*")))

    def test_bounds_cleanup_and_existing_destination(self):
        for actors, prefixes in [
            ([("x" * 8193, b"")], []),
            ([], [("x" * 8193, b"")]),
            ([("x", b"x" * (16 * 1024 * 1024))], []),
        ]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source(root / "source.db", actors, prefixes)
                with self.assertRaises(ValueError):
                    build(root / "source.db", root / "output")
                self.assertFalse((root / "output").exists())
                self.assertFalse(list(root.glob(".px05-build-*")))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source(root / "source.db", [], [])
            (root / "output").mkdir()
            (root / "output/sentinel").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                build(root / "source.db", root / "output")
            self.assertEqual((root / "output/sentinel").read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()

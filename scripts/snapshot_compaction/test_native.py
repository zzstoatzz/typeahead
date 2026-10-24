import hashlib
from pathlib import Path
import random
import sqlite3
import struct
import tempfile
import unittest

from build import build
from native import pack, verify
from reference_build import build as reference_build
from test_build import source
from verify import iter_new


class Tests(unittest.TestCase):
    def test_file_verification_crosses_read_windows_and_oversized_values(self):
        rng = random.Random(1792880000)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "source.db"
            side = root / "map"
            actors = [(f"did:{i:06d}", rng.randbytes(4096)) for i in range(600)]
            source(db, actors, [("large", rng.randbytes(2 * 1024 * 1024 + 17))])
            stats = pack(db, side, root / "pages")
            original = side.read_bytes()
            self.assertGreater(len(original), 4 * 1024 * 1024)
            self.assertGreater(stats.oversized_pages, 0)
            digest = hashlib.sha256(original).hexdigest()
            verify(db, side, digest, stats)
            for position in (1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, len(original) - 1):
                with self.subTest(position=position):
                    corrupt = bytearray(original)
                    corrupt[position] ^= 1
                    side.write_bytes(corrupt)
                    with self.assertRaises(ValueError):
                        verify(db, side, digest, stats)
            side.write_bytes(original)
            verify(db, side, digest, stats)

    def test_byte_identical_to_python_reference(self):
        rng = random.Random(1791518400)
        for case in range(16):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                blobs = [rng.randbytes(n) for n in (0, 1, 3, 30, 127, 255, 1000, 8000)]
                rows = []
                for namespace in range(2):
                    values = [
                        (f"{i:06d}:" + rng.choice(("common-prefix" * 10, "東京", "é", "\0")), rng.choice(blobs))
                        for i in range(case * 101)
                    ]
                    values += [("", b""), ("z" * 8192, rng.randbytes(65536 + namespace))]
                    rows.append(values)
                source(root / "source.db", *rows)
                old = reference_build(root / "source.db", root / "reference")
                new = build(root / "source.db", root / "native")
                self.assertEqual(old, new)
                for filename in ("index.db", "index.db.prefix"):
                    self.assertEqual((root / "reference" / filename).read_bytes(), (root / "native" / filename).read_bytes())
                self.assertEqual(len(list(iter_new((root / "native/index.db.prefix").read_bytes()))), new["keys"])

    def test_rehashed_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "source.db"
            side = root / "map"
            source(db, [(f"did:{i:04d}", b"actor" * 10) for i in range(2000)], [("prefix", b"posting")])
            stats = pack(db, side, root / "pages")
            original = side.read_bytes()
            page = struct.unpack_from("<Q", original, 32)[0]
            key_end = struct.unpack_from("<I", original, page + 4)[0]
            for position in (0, 12, 24, 32, 40, 80, page, page + 4, page + 16, page + key_end + 4):
                with self.subTest(position=position):
                    corrupt = bytearray(original)
                    corrupt[position] ^= 1
                    side.write_bytes(corrupt)
                    with self.assertRaises(ValueError):
                        verify(db, side, hashlib.sha256(corrupt).hexdigest(), stats)
            for length in (0, 79, 83, page - 1, page + 8191, len(original) - 1):
                with self.subTest(length=length):
                    side.write_bytes(original[:length])
                    with self.assertRaises(ValueError):
                        verify(db, side, hashlib.sha256(original[:length]).hexdigest(), stats)
            side.write_bytes(original)
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE prefix_top_base SET dids=?", (b"changed",))
            with self.assertRaises(ValueError):
                verify(db, side, hashlib.sha256(original).hexdigest(), stats)

    def test_non_blob_source_is_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source(root / "source.db", [("did:test", "text, not a blob")], [])
            with self.assertRaises(ValueError):
                build(root / "source.db", root / "output")
            self.assertFalse((root / "output").exists())
            self.assertFalse(list(root.glob(".px05-build-*")))


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from build import digest_file
from convert import convert, FIELDS
from test_build import source
from verify import iter_new


class Tests(unittest.TestCase):
    def fixture(self, root):
        database = root / 'legacy.db'
        source(database, [('did:test', b'opaque actor')], [('h:test', b'opaque posting')])
        manifest = dict(
            manifest_version=1, active_build='original', previous_build=None,
            promoted_at=100, snapshot_url='builds/original/index.db',
            builder_version='test', normalize_version='test', scoring_version='test',
            **dict.fromkeys(FIELDS, 1),
        )
        manifest['source_watermark'] = 100
        with sqlite3.connect(database) as db:
            for field in (*FIELDS, 'normalize_version', 'scoring_version'):
                db.execute('INSERT INTO index_meta VALUES(?,?)', (field, str(manifest[field])))
        manifest.update(byte_size=database.stat().st_size, sha256=digest_file(database))
        path = root / 'legacy.json'
        path.write_text(json.dumps(manifest) + '\n')
        return database, path, manifest

    def test_pair_is_exact_and_legacy_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, path, legacy = self.fixture(root)
            before = database.read_bytes(), path.read_bytes()
            result = convert(database, path, root / 'compact', 'compact-1', 'staging/test')
            self.assertEqual((database.read_bytes(), path.read_bytes()), before)
            self.assertEqual(result, json.loads((root / 'compact/manifest.json').read_bytes()))
            self.assertEqual(result['legacy_twin']['source_watermark'], 100)
            self.assertEqual(result['legacy_twin']['manifest']['sha256'], digest_file(path))
            self.assertEqual(result['legacy_twin']['manifest']['byte_size'], len(before[1]))
            self.assertEqual(result['sha256'], digest_file(root / 'compact/index.db'))
            self.assertEqual(result['prefix_map']['sha256'], digest_file(root / 'compact/index.db.prefix'))
            self.assertEqual(list(iter_new((root / 'compact/index.db.prefix').read_bytes())),
                             [(b'\1did:test', b'opaque actor'), (b'\2h:test', b'opaque posting')])
            with sqlite3.connect(root / 'compact/index.db') as db:
                meta = dict(db.execute('SELECT key,value FROM index_meta'))
            self.assertEqual(meta['build_id'], 'compact-1')
            self.assertEqual(meta['source_watermark'], '100')
            self.assertFalse(list(root.glob('.compact-release-*')))

    def test_bad_sources_never_publish_a_generation(self):
        for fault in ('watermark', 'count', 'checksum', 'identity', 'journal', 'key'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                database, path, legacy = self.fixture(root)
                if fault == 'watermark':
                    legacy['source_watermark'] = 99
                if fault == 'count':
                    with sqlite3.connect(database) as db:
                        db.execute("DELETE FROM actor_compact_base")
                    legacy.update(byte_size=database.stat().st_size, sha256=digest_file(database))
                if fault == 'checksum':
                    legacy['sha256'] = '0' * 64
                if fault == 'journal':
                    Path(str(database) + '-journal').touch()
                path.write_text(json.dumps(legacy))
                with self.assertRaises(ValueError):
                    convert(database, path, root / 'compact',
                            'original' if fault == 'identity' else 'compact-1',
                            '../escape' if fault == 'key' else 'staging/test')
                self.assertFalse((root / 'compact').exists())
                self.assertFalse(list(root.glob('.compact-release-*')))

    def test_existing_generation_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, path, _ = self.fixture(root)
            output = root / 'compact'
            output.mkdir()
            (output / 'keep').write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                convert(database, path, output, 'compact-1', 'staging/test')
            self.assertEqual((output / 'keep').read_bytes(), b'existing')


if __name__ == '__main__':
    unittest.main()

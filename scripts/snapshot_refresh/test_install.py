import importlib.util
import json
import subprocess
import sys
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('snapshot_install', Path(__file__).with_name('install.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_managed_startup_rejects_missing_telemetry_before_touching_storage(self):
        env = {k: v for k, v in os.environ.items() if k not in ("LOGFIRE_TOKEN", "LOGFIRE_WRITE_TOKEN")}
        env["FLY_APP_NAME"] = "typeahead-search-refresh"
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("install.py"))], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Managed search requires a Logfire write token", result.stderr)

    def manifest(self):
        bid = 'b1792908655-test-px05'
        return dict(manifest_version=2, format_version=5, active_build=bid, source_watermark=int(time.time()) - 60,
                    actor_count=10, prefix_count=50, byte_size=8192, sha256='a' * 64,
                    snapshot_url='releases/search/' + bid + '/index.db',
                    prefix_map=dict(byte_size=16384, sha256='b' * 64, object_key='releases/search/' + bid + '/index.db.prefix'),
                    normalize_version='v2-cjk-bigram', scoring_version='v1-quality-score', top_n_short=300,
                    top_n_medium=200, top_n_long=100, overlay_top_n=100, hydrate_cap=50, cjk_key_count=50000, cjk_actor_count=1)

    def test_manifest_rejects_stale_sources_changed_generation_and_artifact_paths(self):
        m = self.manifest()
        installer.validate_manifest(m, m['active_build'])
        for key, value in [('source_watermark', 100), ('source_watermark', int(time.time()) + 300),
                           ('active_build', '../../other'), ('snapshot_url', 'releases/other/index.db')]:
            with self.subTest(key=key, value=value):
                bad = dict(m, **{key: value})
                with self.assertRaises(ValueError):
                    installer.validate_manifest(bad, m['active_build'])
        for value in ['../other', 'https://example.com/file', 'control/./other', 'control//other', 'control/other\n']:
            with self.assertRaises(ValueError):
                installer.object_key(value)

    def test_database_metadata_is_checked_against_real_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.db'
            m = self.manifest()
            metadata = {k: str(v) for k, v in m.items() if not isinstance(v, dict)}
            metadata.update(build_id=m['active_build'], flat_prefix_version='5', flat_prefix_sha256=m['prefix_map']['sha256'], pds_scope_version='1')
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE index_meta(key TEXT PRIMARY KEY,value TEXT)')
                db.executemany('INSERT INTO index_meta VALUES(?,?)', metadata.items())
            installer.validate_database(path, m)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE index_meta SET value='0' WHERE key='source_watermark'")
            with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
                installer.validate_database(path, m)
            path.write_bytes(b'not a SQLite snapshot')
            with self.assertRaises(sqlite3.DatabaseError):
                installer.validate_database(path, m)

    def test_atomic_activation_replaces_incomplete_temporary_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index/active.json'
            installer.atomic_json(path, {'current': 'old'})
            path.with_suffix('.tmp').write_bytes(b'{incomplete')
            self.assertEqual(json.loads(path.read_text()), {'current': 'old'})
            installer.atomic_json(path, {'current': 'verified'})
            self.assertEqual(json.loads(path.read_text()), {'current': 'verified'})
            self.assertFalse(path.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()

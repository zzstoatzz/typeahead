"""SQLite lock handling of the real comparator executable; tiny local fixtures."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get('TYPEAHEAD_LOOKUP_TEST_BINARY'), 'Explicit executable required')
class LookupLocks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = sqlite3.connect(root / 'overlay.db')
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE prefix_overlay(key TEXT,did TEXT,score REAL,op INTEGER);
          CREATE TABLE actor_overlay(did TEXT,blob BLOB,hidden INTEGER);
        ''')
        snapshot = sqlite3.connect(root / 'index.db')
        snapshot.executescript('''
          CREATE TABLE prefix_top_base(key TEXT,dids BLOB);
          CREATE TABLE actor_compact_base(did TEXT,blob BLOB);
        ''')
        snapshot.close()
        self.env = {'PATH': '/usr/bin:/bin',
                    'TYPEAHEAD_EVAL_OVERLAY': str(root / 'overlay.db'),
                    'TYPEAHEAD_EVAL_SNAPSHOT': str(root / 'index.db'),
                    'TYPEAHEAD_EVAL_QUERY': 'alice'}
        self.db.execute('PRAGMA locking_mode=EXCLUSIVE')
        self.db.execute('BEGIN EXCLUSIVE')
        self.db.execute('SELECT count(*) FROM actor_overlay').fetchone()
        self.child = subprocess.Popen([os.environ['TYPEAHEAD_LOOKUP_TEST_BINARY']],
                                      env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)

    def tearDown(self):
        self.db.close()
        if self.child.poll() is None:
            self.child.kill()
            self.child.communicate()
        self.temp.cleanup()

    def test_brief_lock_waits_then_succeeds(self):
        time.sleep(.15)
        self.assertIsNone(self.child.poll(), 'Comparator failed instead of waiting')
        self.db.rollback()
        self.db.close()
        output, error = self.child.communicate(timeout=3)
        self.assertEqual(self.child.returncode, 0, error)
        self.assertTrue(json.loads(output)['equal'])

    def test_persistent_lock_fails_within_deadline(self):
        output, error = self.child.communicate(timeout=3)
        self.assertNotEqual(self.child.returncode, 0)
        self.assertEqual(output, '')
        self.assertIn('error: Busy', error)


if __name__ == '__main__':
    unittest.main()

import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_identity_discovery import Discovery, SELECT_SQL
from pds_identity_queue import IdentityQueue


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'queue.db'
        self.source = sqlite3.connect(':memory:')
        self.addCleanup(self.source.close)
        self.source.executescript('''CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,
            updated_at INTEGER,handle TEXT,hidden INTEGER);
            CREATE INDEX idx_actors_updated_at ON actors(updated_at,did);''')
        self.source.executemany('INSERT INTO actors VALUES(?,?,?,?,?)', [
            ('did:plc:a', '', 10, '', 1), ('did:plc:b', 'https://known.test', 10, 'b.test', 0),
            ('did:plc:c', '', 10, 'c.test', 0), ('did:plc:d', '', 11, 'd.test', 0)])
        self.discovery = Discovery(self.path)
        self.discovery.begin(10, 11)

    def test_ties_hidden_handleless_and_restart(self):
        for _ in range(4):
            request = self.discovery.request(cap=1)
            rows = self.source.execute(SELECT_SQL, request).fetchall()
            self.discovery.accept(request, rows, 100)
            self.discovery = Discovery(self.path)
        self.assertIsNone(self.discovery.request())
        claims = IdentityQueue(self.path).claim(100, limit=10, lease_seconds=60)
        self.assertEqual([r['did'] for r in claims], ['did:plc:a', 'did:plc:c'])
        self.discovery.begin(10, 12)
        self.assertIsNotNone(self.discovery.request())

    def test_stale_or_malformed_page_cannot_advance_or_enqueue(self):
        request = self.discovery.request()
        with self.assertRaises(ValueError):
            self.discovery.accept(request, [('did:plc:c', '', 10), ('did:plc:a', '', 10)], 100)
        self.assertEqual(self.discovery.request(), request)
        self.assertEqual(IdentityQueue(self.path).claim(100, limit=10, lease_seconds=60), [])
        rows = self.source.execute(SELECT_SQL, request).fetchall()
        self.discovery.accept(request, rows, 100)
        with self.assertRaisesRegex(ValueError, 'cursor changed'):
            self.discovery.accept(request, rows, 100)

    def test_refuses_gaps_and_incomplete_window_replacement(self):
        with self.assertRaises(ValueError):
            self.discovery.begin(10, 12)
        request = self.discovery.request()
        self.discovery.accept(request, [], 100)
        with self.assertRaises(ValueError):
            self.discovery.begin(12, 13)

    def test_source_selector_is_bounded_index_search(self):
        plans = self.source.execute('EXPLAIN QUERY PLAN ' + SELECT_SQL,
                                    self.discovery.request()).fetchall()
        self.assertTrue(any('SEARCH actors USING INDEX idx_actors_updated_at' in row[3] for row in plans))
        self.assertFalse(any('SCAN actors' in row[3] for row in plans))


if __name__ == '__main__':
    unittest.main()

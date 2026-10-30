import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_cloud_batch import discover
from pds_freshness_queue import Queue


class DiscoveryTests(unittest.TestCase):
    def test_bounded_scan_resumes_without_losing_page_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            db = sqlite3.connect(':memory:')
            self.addCleanup(db.close)
            db.execute('CREATE TABLE actors(did TEXT,handle TEXT,pds TEXT,updated_at INTEGER,hidden INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?)', [(f'did:plc:{i}', f'{i}.test', '' if i%2 else 'https://known.test', 1, 0) for i in range(100)])
            def execute(sql, params):
                return db.execute(sql, params).fetchall()
            queue = Queue(Path(temp)/'queue.db')
            first, count = discover(execute, queue, 5)
            self.assertEqual(count, 10)
            self.assertEqual(queue.cursor(), 10)
            second, count = discover(execute, queue, 5)
            self.assertEqual(count, 10)
            self.assertEqual(queue.cursor(), 20)
            self.assertFalse(set(x['did'] for x in first) & set(x['did'] for x in second))
            plan = execute('EXPLAIN QUERY PLAN SELECT rowid FROM actors WHERE rowid>:cursor ORDER BY rowid LIMIT :limit', {'cursor':0,'limit':1000})
            self.assertIn('SEARCH', plan[0][3])


if __name__ == '__main__':
    unittest.main()

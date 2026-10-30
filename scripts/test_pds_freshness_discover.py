import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from pds_freshness_discover import PAGE_SQL, discover
from pds_freshness_queue import Queue


class DiscoveryTests(unittest.TestCase):
    def test_checkpointed_pages_and_repeat_do_not_reset_verification(self):
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE actors(did TEXT,pds TEXT)')
            dids = ['did:plc:' + letter * 24 for letter in ('a', 'b', 'c')]
            db.executemany('INSERT INTO actors VALUES(?,?)', [(dids[0], ''), (dids[1], 'https://known.test'), ('did:plc:bad', ''), (dids[2], '')])
            def execute(sql, params):
                return db.execute(sql, params).fetchall()
            queue = Queue(Path(directory)/'queue.db')
            first = discover(execute, queue, pages=1, page_size=2)
            self.assertEqual((first['cursor'], first['inserted']), (2, 2))
            claim = queue.claim(limit=1)[0]
            queue.verified(claim['did'], claim['token'], 'https://resolved.test', int(time.time()))
            restarted = Queue(Path(directory)/'queue.db')
            second = discover(execute, restarted, pages=2, page_size=2)
            self.assertEqual((second['cursor'], second['inserted'], second['excluded']), (4, 1, 1))
            self.assertTrue(second['caught_up'])
            self.assertEqual(restarted.metrics()['verified_count'], 1)
            plan = execute('EXPLAIN QUERY PLAN '+PAGE_SQL, {'cursor':2,'limit':2})
            self.assertIn('SEARCH actors USING INTEGER PRIMARY KEY', str(plan))

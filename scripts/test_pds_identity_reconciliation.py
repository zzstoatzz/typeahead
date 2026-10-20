import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_identity_reconciliation import Reconciliation, SELECT_SQL
from pds_identity_queue import IdentityQueue


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'queue.db'
        self.source=sqlite3.connect(Path(tmp.name)/'source.db');self.addCleanup(self.source.close)
        self.source.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,updated_at INTEGER,handle TEXT,hidden INTEGER)')
        self.source.executemany('INSERT INTO actors VALUES(?,?,?,?,?)',[
            ('did:plc:known','https://pds.test',10,'known.test',0),
            ('did:plc:old','',1,'',1),
            ('did:plc:recent','',100,'recent.test',0)])
        self.sweep=Reconciliation(self.path);self.sweep.begin(1,3)

    def page(self,cap=2):
        request=self.sweep.request(cap)
        return request,self.source.execute(SELECT_SQL,request).fetchall()

    def test_bounded_pages_cover_hidden_handleless_backdated_rows_after_restart(self):
        request,rows=self.page();self.assertEqual(len(rows),2)
        self.assertEqual(self.sweep.accept(request,rows,1000),1)
        self.sweep=Reconciliation(self.path)
        request,rows=self.page();self.sweep.accept(request,rows,1001)
        request,rows=self.page();self.sweep.accept(request,rows,1002)
        self.assertIsNone(self.sweep.request())
        claims=IdentityQueue(self.path).claim(1002,limit=10,lease_seconds=60)
        self.assertEqual({c['did'] for c in claims},{'did:plc:old','did:plc:recent'})
        plan=self.source.execute('EXPLAIN QUERY PLAN '+SELECT_SQL,{'after':0,'end':3,'cap':2}).fetchall()
        self.assertTrue(any('SEARCH actors USING INTEGER PRIMARY KEY' in r[3] for r in plan))

    def test_stale_receipt_and_invalid_page_leave_cursor_unchanged(self):
        request,rows=self.page()
        with self.assertRaises(ValueError):self.sweep.accept(request,list(reversed(rows)),1000)
        self.assertEqual(self.sweep.request(2),request)
        self.sweep.accept(request,rows,1000)
        with self.assertRaisesRegex(ValueError,'cursor changed'):self.sweep.accept(request,rows,1001)
        with self.assertRaises(ValueError):self.sweep.begin(2,3)

    def test_next_cycle_finds_backdated_change_behind_completed_cursor(self):
        while self.sweep.request():
            request,rows=self.page();self.sweep.accept(request,rows,1000)
        queue=IdentityQueue(self.path)
        for claim in queue.claim(1000,limit=10,lease_seconds=60):queue.finish(claim,1001,verified=True)
        self.source.execute("UPDATE actors SET updated_at=0 WHERE did='did:plc:recent'")
        self.sweep.begin(2,3)
        while self.sweep.request():
            request,rows=self.page();self.sweep.accept(request,rows,1002)
        self.assertIn('did:plc:recent',{c['did'] for c in queue.claim(1002,limit=10,lease_seconds=60)})

    def test_insert_above_boundary_waits_until_next_cycle(self):
        self.source.execute("INSERT INTO actors VALUES('did:plc:new','',0,'',1)")
        while self.sweep.request():
            request,rows=self.page();self.sweep.accept(request,rows,1000)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identity_jobs WHERE did='did:plc:new'").fetchone()[0],0)
        self.sweep.begin(2,4)
        while self.sweep.request():
            request,rows=self.page();self.sweep.accept(request,rows,1001)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identity_jobs WHERE did='did:plc:new'").fetchone()[0],1)


if __name__=='__main__':unittest.main()

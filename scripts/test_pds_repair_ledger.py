import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_repair_ledger import Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.ledger=Ledger(Path(tmp.name)/'ledger.db')
        self.db=sqlite3.connect(Path(tmp.name)/'actors.db');self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,updated_at INTEGER);INSERT INTO actors VALUES('did:plc:a','',10);")
        self.attempt=self.ledger.prepare('did:plc:a',10,'https://pds.test')

    def execute(self, sql, params):
        with self.db:return self.db.execute(sql,params).fetchall()

    def current(self):
        row=self.db.execute("SELECT pds,updated_at FROM actors WHERE did='did:plc:a'").fetchone()
        return None if row is None else dict(zip(('pds','updated_at'),row))

    def test_confirmed_write_cannot_be_resent(self):
        self.assertEqual(self.ledger.send(self.attempt,self.execute),'confirmed')
        self.assertEqual(self.current()['pds'],'https://pds.test')
        with self.assertRaises(ValueError):self.ledger.send(self.attempt,self.execute)
        with self.assertRaises(sqlite3.IntegrityError):self.ledger.prepare('did:plc:a',10,'https://pds.test')

    def test_commit_then_lost_receipt_reconciles_without_claiming_ownership(self):
        def commit_then_disconnect(sql, params):
            self.execute(sql,params)
            raise ConnectionError('Receipt lost after commit')
        with self.assertRaises(ConnectionError):self.ledger.send(self.attempt,commit_then_disconnect)
        self.assertEqual(self.ledger.get(self.attempt)['state'],'unknown')
        restarted=Ledger(self.ledger.path)
        self.assertEqual(restarted.reconcile(self.attempt,self.current()),'present_unattributed')
        with self.assertRaises(ValueError):restarted.send(self.attempt,self.execute)

    def test_batch_updates_only_unchanged_empty_rows(self):
        self.db.executescript("INSERT INTO actors VALUES('did:plc:b','https://moved.test',10);INSERT INTO actors VALUES('did:plc:c','',11);")
        second=self.ledger.prepare('did:plc:b',10,'https://pds.test')
        third=self.ledger.prepare('did:plc:c',10,'https://pds.test')
        result=self.ledger.send_batch([self.attempt,second,third],self.execute)
        self.assertEqual(result,{'confirmed':1,'conflict':2})
        self.assertEqual(self.db.execute('SELECT pds FROM actors ORDER BY did').fetchall(),[('https://pds.test',),('https://moved.test',),('',)])
        with self.assertRaises(ValueError):self.ledger.send_batch([self.attempt,second],self.execute)

    def test_batch_commit_with_lost_response_is_not_replayed(self):
        self.db.executescript("INSERT INTO actors VALUES('did:plc:b','',10);")
        second=self.ledger.prepare('did:plc:b',10,'https://pds.test')
        def interrupted(sql,params):
            self.execute(sql,params)
            raise ConnectionError('Receipt lost')
        with self.assertRaises(ConnectionError):self.ledger.send_batch([self.attempt,second],interrupted)
        for attempt in [self.attempt,second]:self.assertEqual(self.ledger.get(attempt)['state'],'unknown')
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='https://pds.test'").fetchone(),(2,))

    def test_interrupted_before_send_and_concurrent_migration(self):
        self.ledger.transition(self.attempt,'prepared','sent')
        self.assertEqual(Ledger(self.ledger.path).reconcile(self.attempt,self.current()),'unchanged')
        with self.assertRaises(ValueError):self.ledger.send(self.attempt,self.execute)
        self.db.execute("UPDATE actors SET pds='https://new.test',updated_at=11")
        self.db.commit()
        other=self.ledger.prepare('did:plc:b',10,'https://pds.test')
        self.assertEqual(self.ledger.send(other,self.execute),'conflict')
        self.assertEqual(self.current()['pds'],'https://new.test')


if __name__=='__main__':unittest.main()

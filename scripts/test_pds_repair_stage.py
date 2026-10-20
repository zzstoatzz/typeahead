import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pds_repair_stage import run,READ_SQL


class StageTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.db=sqlite3.connect(self.root/'source.db');self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,updated_at INTEGER); CREATE INDEX freshness ON actors(updated_at,did);')
        self.items=[dict(did='did:plc:'+c*24,pds='https://community.test',provenance={'cid':c,'tail':'qualified-test-tail'}) for c in 'abc']
        self.db.executemany('INSERT INTO actors VALUES(?,?,?)',[(r['did'],'',10) for r in self.items]);self.db.commit()
        self.ledger=self.root/'ledger.db';self.output=self.root/'stage'

    def execute(self,sql,params):
        with self.db:return self.db.execute(sql,params).fetchall()

    def run_stage(self,**kwargs):
        defaults=dict(candidates=self.items,output=self.output,ledger_path=self.ledger,
            execute=self.execute,admit=lambda:{'test_source':'isolated SQLite'},batch_size=2,
            max_rows=3,interval_seconds=.001,max_write_seconds=1)
        return run(**(defaults|kwargs))

    def test_bounded_live_lookup_uses_actor_index(self):
        details=[r[3] for r in self.db.execute('EXPLAIN QUERY PLAN '+READ_SQL,
            {'dids':json.dumps([r['did'] for r in self.items])})]
        self.assertTrue(any('SEARCH actors USING INDEX' in d for d in details),details)
        self.assertFalse(any('SCAN actors' in d for d in details),details)

    def test_real_partial_conflict_preserves_other_identity(self):
        calls=0
        def admit():
            nonlocal calls
            calls+=1
            if calls==2:
                self.db.execute('UPDATE actors SET pds=?,updated_at=11 WHERE did=?',('https://moved.test',self.items[1]['did']));self.db.commit()
            return {'gate':calls}
        self.assertEqual(self.run_stage(admit=admit),{'confirmed':2,'conflicts':1,'skipped':0})
        self.assertEqual(self.db.execute('SELECT pds FROM actors ORDER BY did').fetchall(),[('https://community.test',),('https://moved.test',),('https://community.test',)])
        with self.assertRaises(FileExistsError):self.run_stage()

    def test_lost_receipt_stops_and_cannot_replay_ambiguous_empty_row(self):
        def disconnected(sql,params):
            result=self.execute(sql,params)
            if sql.startswith('UPDATE'):raise ConnectionError('Lost committed response')
            return result
        with self.assertRaises(ConnectionError):self.run_stage(execute=disconnected)
        with sqlite3.connect(self.ledger) as db:
            self.assertEqual(db.execute('SELECT state FROM attempts').fetchall(),[('unknown',),('unknown',)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='' ").fetchone()[0],1)
        self.db.execute('UPDATE actors SET pds=? WHERE did=?',('',self.items[0]['did']));self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.run_stage(output=self.root/'second')
        self.assertEqual(self.db.execute('SELECT pds FROM actors WHERE did=?',(self.items[0]['did'],)).fetchone(),('',))

    def test_slow_real_write_stops_before_next_batch(self):
        def slow(sql,params):
            result=self.execute(sql,params)
            if sql.startswith('UPDATE'):time.sleep(.02)
            return result
        with self.assertRaisesRegex(RuntimeError,'latency budget'):
            self.run_stage(execute=slow,max_write_seconds=.01,batch_size=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='' ").fetchone()[0],2)

    def test_failed_admission_and_stop_file_prevent_write(self):
        calls=0
        def stop():
            nonlocal calls
            calls+=1
            if calls==1:(self.output/'STOP').touch()
            return {'storage':'must stop'}
        with self.assertRaisesRegex(RuntimeError,'stop file'):self.run_stage(admit=stop)
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='' ").fetchone()[0],3)
        def failed():raise RuntimeError('Disk reserve reached')
        with self.assertRaisesRegex(RuntimeError,'Disk reserve'):
            self.run_stage(output=self.root/'storage-stop',admit=failed)
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='' ").fetchone()[0],3)

    def test_deleted_and_already_known_rows_are_not_restored(self):
        self.db.execute('DELETE FROM actors WHERE did=?',(self.items[0]['did'],))
        self.db.execute('UPDATE actors SET pds=? WHERE did=?',('https://existing.test',self.items[1]['did']));self.db.commit()
        self.assertEqual(self.run_stage(),{'confirmed':1,'conflicts':0,'skipped':2})
        self.assertEqual(self.db.execute('SELECT count(*) FROM actors').fetchone()[0],2)

    def test_rejects_unbounded_or_unproven_input_before_stage_creation(self):
        for changes in [dict(batch_size=101),dict(max_rows=2),dict(max_write_seconds=float('nan')),
                        dict(candidates=self.items+[self.items[0]],max_rows=4),
                        dict(candidates=[self.items[0]|{'provenance':None}])]:
            with self.assertRaises(ValueError):self.run_stage(**changes)
            self.assertFalse(self.output.exists())


if __name__=='__main__':unittest.main()

import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from pds_reconstruction_report import report


class ReportTests(unittest.TestCase):
    def test_full_accounting_and_incomplete_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'queue.db'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript("""
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    INSERT INTO metadata VALUES('complete','true'),('selected','5'),
                      ('plc_reconstruction_complete','true'),('plc_tail_complete','false'),
                      ('source','offline.db'),('plc_tail_cutoff','2026-10-20T19:00:00Z');
                    CREATE TABLE candidates(did TEXT PRIMARY KEY);
                    INSERT INTO candidates VALUES('did:plc:a'),('did:plc:b'),
                      ('did:plc:c'),('did:plc:d'),('did:web:example.com');
                    CREATE TABLE plc_latest(did TEXT PRIMARY KEY,pds TEXT,status TEXT);
                    INSERT INTO plc_latest VALUES('did:plc:a','https://pds.test','resolved'),
                      ('did:plc:b',NULL,'unresolved'),('did:plc:c',NULL,'tombstoned');
                """)
            with self.assertRaises(ValueError):report(path)
            with closing(sqlite3.connect(path)) as db,db:
                db.execute("UPDATE metadata SET value='true' WHERE key='plc_tail_complete'")
            result=report(path)
            self.assertEqual(result['candidates'],5)
            self.assertEqual(result['states'],dict(resolved=1,unresolved=1,tombstoned=1,absent_from_plc=1,non_plc=1))
            self.assertFalse(result['apply_ready'])
            self.assertEqual(result['endpoints'],[dict(pds='https://pds.test',candidates=1)])
            with closing(sqlite3.connect(path)) as db,db:
                db.execute("UPDATE plc_latest SET pds=NULL WHERE status='resolved'")
            with self.assertRaises(ValueError):report(path)

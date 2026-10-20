from contextlib import closing
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from pds_coverage import APPLY_SQL, ROLLBACK_SQL, pds_from_document, prepare, record_resolution


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.db'
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,pds TEXT,updated_at INTEGER,
            display_name TEXT,quality_score REAL,hidden INTEGER);
          CREATE TABLE fts_changes(did TEXT);
          CREATE TRIGGER actors_au AFTER UPDATE ON actors
            WHEN old.handle <> new.handle OR old.display_name <> new.display_name
            BEGIN INSERT INTO fts_changes VALUES(new.did); END;
          INSERT INTO actors VALUES('did:plc:a','a.test','',10,'Alice',123,0);
          INSERT INTO actors VALUES('did:plc:b','b.test','https://b.test',11,'Bob',456,1);
        ''')
        self.queue = Path(self.tmp.name) / 'queue.db'
        self.params = dict(did='did:plc:a',pds='https://pds.test',observed_updated_at=10,written_at=20,rollback_at=30)

    def test_offline_selection_and_provenance(self):
        self.assertEqual(prepare(self.source,self.queue),1)
        with closing(sqlite3.connect(self.queue)) as q:
            self.assertEqual(q.execute('select did,observed_updated_at from candidates').fetchall(),[('did:plc:a',10)])
            self.assertEqual(q.execute("select value from metadata where key='complete'").fetchone(),('true',))
            doc={'id':'did:plc:a','service':[{'id':'#atproto_pds','type':'AtprotoPersonalDataServer','serviceEndpoint':'https://pds.test/'}]}
            self.assertEqual(record_resolution(q,'did:plc:a',doc,15),'https://pds.test')
            self.assertEqual(q.execute('select state,resolved_at from candidates').fetchone(),('resolved',15))
        with self.assertRaises(ValueError): prepare(self.source,self.queue)

    def test_snapshot_candidates_have_no_invented_live_version(self):
        snapshot=Path(self.tmp.name)/'snapshot.db'
        def actor(pds):
            fields=[b'a.test',b'Alice',b'',b'[]',pds,b'',b'{}']
            return bytes([1,0])+struct.pack('<f',10)+b''.join(struct.pack('<H',len(f))+f for f in fields)
        with closing(sqlite3.connect(snapshot)) as db,db:
            db.executescript("CREATE TABLE index_meta(key TEXT,value TEXT);INSERT INTO index_meta VALUES('build_id','test-build'),('source_watermark','123');CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY,blob BLOB);")
            db.executemany('INSERT INTO actor_compact_base VALUES(?,?)',[('did:plc:a',actor(b'')),('did:plc:b',actor(b'https://known.test'))])
        self.assertEqual(prepare(snapshot,self.queue,source_format='snapshot'),1)
        with closing(sqlite3.connect(self.queue)) as db:
            self.assertEqual(db.execute('SELECT did,observed_updated_at FROM candidates').fetchall(),[('did:plc:a',None)])
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='version_provenance'").fetchone(),('live_read_required',))
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='build_id'").fetchone(),('test-build',))

    def test_apply_retry_and_guarded_rollback(self):
        self.assertEqual(len(self.db.execute(APPLY_SQL,self.params).fetchall()),1)
        self.assertEqual(self.db.execute(APPLY_SQL,self.params).fetchall(),[])
        self.assertEqual(self.db.execute('select handle,display_name,quality_score,hidden from actors where did=?',('did:plc:a',)).fetchone(),('a.test','Alice',123,0))
        self.assertEqual(self.db.execute('select * from fts_changes').fetchall(),[])
        self.assertEqual(len(self.db.execute(ROLLBACK_SQL,self.params).fetchall()),1)
        self.assertEqual(self.db.execute(ROLLBACK_SQL,self.params).fetchall(),[])

    def test_concurrent_updates_and_deletion_win(self):
        for change in ["UPDATE actors SET updated_at=11 WHERE did='did:plc:a'", "UPDATE actors SET pds='https://moved.test' WHERE did='did:plc:a'", "DELETE FROM actors WHERE did='did:plc:a'"]:
            self.db.execute('SAVEPOINT scenario')
            self.db.execute(change)
            self.assertEqual(self.db.execute(APPLY_SQL,self.params).fetchall(),[])
            self.db.execute('ROLLBACK TO scenario')
            self.db.execute('RELEASE scenario')
        self.db.execute(APPLY_SQL,self.params).fetchall()
        self.db.execute("UPDATE actors SET updated_at=21 WHERE did='did:plc:a'")
        self.assertEqual(self.db.execute(ROLLBACK_SQL,self.params).fetchall(),[])

    def test_identity_endpoints_are_not_limited_by_filter_hostname_policy(self):
        for endpoint in ['http://pds.test','https://pds.test:8443','https://pds.test/prefix']:
            doc={'id':'did:plc:a','service':[{'id':'#atproto_pds','type':'AtprotoPersonalDataServer','serviceEndpoint':endpoint}]}
            self.assertEqual(pds_from_document('did:plc:a',doc),endpoint)

    def test_reject_wrong_subject_ambiguous_and_bad_endpoints(self):
        service={'id':'#atproto_pds','type':'AtprotoPersonalDataServer','serviceEndpoint':'https://pds.test'}
        docs=[{'id':'did:plc:wrong','service':[service]}, {'id':'did:plc:a','service':[]}, {'id':'did:plc:a','service':[service,service]}]
        docs += [{'id':'did:plc:a','service':[{**service,'serviceEndpoint':url}]} for url in ['ftp://pds.test','https://user@pds.test','https://pds.test:invalid','https://pds.test#fragment','https://pds.test?x=1']]
        for doc in docs:
            with self.assertRaises(ValueError): pds_from_document('did:plc:a',doc)


if __name__ == '__main__': unittest.main()

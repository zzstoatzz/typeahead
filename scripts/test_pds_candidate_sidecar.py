import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from pds_candidate_sidecar import augment


class SidecarTests(unittest.TestCase):
    def test_provenance_atomic_duplicates_and_idempotent_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);queue=root/'queue.db';sidecar=root/'sidecar.json'
            with closing(sqlite3.connect(queue)) as db,db:
                db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE candidates(did TEXT PRIMARY KEY,observed_updated_at INTEGER);')
                db.executemany('INSERT INTO metadata VALUES(?,?)',{'complete':'true','source_format':'snapshot','version_provenance':'live_read_required','build_id':'b1','source_watermark':'100','selected':'1'}.items())
                db.execute("INSERT INTO candidates VALUES('did:plc:z',NULL)")
            data={'requires_current_live_read':True,'build_id':'wrong','source_watermark':100,'rows':[['did:plc:a',99],['did:plc:z',98]]}
            sidecar.write_text(json.dumps(data))
            with self.assertRaises(ValueError):augment(queue,sidecar)
            data['build_id']='b1';sidecar.write_text(json.dumps(data))
            with self.assertRaises(sqlite3.IntegrityError):augment(queue,sidecar)
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone(),(1,))
            data['rows']=data['rows'][:1];sidecar.write_text(json.dumps(data))
            self.assertEqual(augment(queue,sidecar)['total'],2)
            self.assertTrue(augment(queue,sidecar)['already_applied'])
            with closing(sqlite3.connect(queue)) as db,db:
                db.execute("INSERT INTO metadata VALUES('plc_reconstruction_complete','false')")
            self.assertTrue(augment(queue,sidecar)['already_applied'])
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute("SELECT observed_updated_at FROM candidates WHERE did='did:plc:a'").fetchone(),(None,))
            data['rows']=[['did:plc:b',98]];sidecar.write_text(json.dumps(data))
            with self.assertRaises(ValueError):augment(queue,sidecar)


if __name__=='__main__':unittest.main()

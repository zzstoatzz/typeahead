import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from pds_coverage import prepare
from pds_plc_merge import merge
from pds_plc_reconstruct import SCHEMA,parse_operation,store_rows


def record(cid,date,kind='plc_operation'):
    return {'did':'did:plc:a','cid':cid,'createdAt':date,'nullified':False,'operation':{'type':kind,'services':{'atproto_pds':{'type':'AtprotoPersonalDataServer','endpoint':'https://pds.test'}}}}


class MergeTests(unittest.TestCase):
    def test_tail_tombstone_and_checksum_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.db';queue=root/'queue.db';tail=root/'tail';tail.mkdir()
            with closing(sqlite3.connect(source)) as db:
                db.executescript("CREATE TABLE actors(did TEXT,pds TEXT,updated_at INTEGER);INSERT INTO actors VALUES('did:plc:a','',1);")
            prepare(source,queue)
            with closing(sqlite3.connect(queue)) as db,db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO metadata VALUES('plc_reconstruction_complete','true')")
                db.execute("INSERT INTO metadata VALUES('plc_bundle_end','1792022400')")
                store_rows(db,[parse_operation(record('a','2026-10-14T01:00:00.000Z'),'bundle')])
            config={'after':'2026-10-14T00:00:00.000Z','cutoff':'2026-10-16T00:00:00.000Z'}
            (tail/'config.json').write_text(json.dumps(config));(tail/'complete.json').write_text(json.dumps({**config,'pages':1}))
            records=[record('b','2026-10-15T01:00:00.000Z','plc_tombstone'),record('c',config['cutoff'])]
            page={'after':config['after'],'response':records,'sha256':hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()}
            (tail/'000000.json').write_text(json.dumps(page))
            self.assertFalse(merge(queue,tail)['apply_ready'])
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute('SELECT status,pds FROM plc_latest').fetchone(),('tombstoned',None))
            page['response'][0]['cid']='tampered';(tail/'000000.json').write_text(json.dumps(page))
            with self.assertRaises(ValueError):merge(queue,tail)
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='plc_tail_complete'").fetchone(),('false',))


if __name__=='__main__':unittest.main()

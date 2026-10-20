from contextlib import closing
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pds_coverage import prepare
from pds_plc_reconstruct import SCHEMA,parse_operation,reconstruct,store_rows


def record(cid='a',date='2026-10-01T00:00:00.000Z',**operation):
    return dict(did='did:plc:a',cid=cid,createdAt=date,nullified=False,operation={'type':'plc_operation','services':{'atproto_pds':{'type':'AtprotoPersonalDataServer','endpoint':'https://pds.test'}},**operation})


class ReconstructTests(unittest.TestCase):
    def test_latest_tombstone_prevents_resurrection(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(SCHEMA)
            newer=record('b','2026-10-02T00:00:00.000Z',type='plc_tombstone')
            store_rows(db,[parse_operation(newer,'bundle'),parse_operation(record(),'bundle')])
            self.assertEqual(db.execute('select pds,status from plc_latest').fetchone(),(None,'tombstoned'))
            store_rows(db,[parse_operation(newer,'bundle')])
            with self.assertRaises(ValueError):store_rows(db,[parse_operation(record('c','2026-10-02T00:00:00.000Z'),'bundle')])

    def test_nullified_legacy_and_invalid_newer(self):
        self.assertIsNone(parse_operation({**record(),'nullified':True},'b'))
        self.assertEqual(parse_operation(record(type='create',service='https://old.test'),'b')[3],'https://old.test')
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(SCHEMA)
            store_rows(db,[parse_operation(record(),'b'),parse_operation(record('b','2026-10-02T00:00:00.000Z',services={}),'b')])
            self.assertEqual(db.execute('select pds,status from plc_latest').fetchone(),(None,'unresolved'))
        r=record();del r['nullified']
        with self.assertRaises(ValueError):parse_operation(r,'b')

    def test_legacy_seconds_normalize_without_changing_order(self):
        old=record('a','2025-10-02T18:12:35Z')
        later=record('b','2025-10-02T18:12:35.001Z')
        self.assertEqual(parse_operation(old,'bundle')[1],'2025-10-02T18:12:35.000Z')
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(SCHEMA)
            store_rows(db,[parse_operation(later,'bundle'),parse_operation(old,'bundle')])
            self.assertEqual(db.execute('SELECT cid FROM plc_latest').fetchone(),('b',))
        for bad in ['2025-10-02T18:12:35','2025-10-02T18:12:35.000001Z']:
            with self.assertRaises(ValueError):parse_operation(record(date=bad),'bundle')

    def test_actual_gzip_resume_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.db';queue=root/'queue.db';bundles=root/'bundles';bundles.mkdir()
            with closing(sqlite3.connect(source)) as db:
                db.executescript("CREATE TABLE actors(did TEXT,pds TEXT,updated_at INTEGER);INSERT INTO actors VALUES('did:plc:a','',1);")
            prepare(source,queue)
            p=bundles/'1668643200.jsonl.gz'
            with gzip.open(p,'wt') as f:f.write(json.dumps(record())+json.dumps(record()))
            reconstruct(queue,bundles);reconstruct(queue,bundles)
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute('select count(*) from plc_latest').fetchone(),(1,))
                self.assertEqual(db.execute('select operations from plc_bundles').fetchone(),(2,))
            bad=bundles/'1669248000.jsonl.gz';bad.write_bytes(b'not gzip')
            with self.assertRaises(gzip.BadGzipFile):reconstruct(queue,bundles)
            with closing(sqlite3.connect(queue)) as db:
                self.assertEqual(db.execute("select value from metadata where key='plc_reconstruction_complete'").fetchone(),('false',))
                self.assertEqual(db.execute('select count(*) from plc_bundles').fetchone(),(1,))


if __name__=='__main__':unittest.main()

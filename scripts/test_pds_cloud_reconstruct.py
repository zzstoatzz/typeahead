import functools
import gzip
import hashlib
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path

from pds_cloud_reconstruct import run, validate_manifest
from pds_coverage import prepare
from test_pds_plc_reconstruct import record


class CloudReconstructionTests(unittest.TestCase):
    def test_interrupted_transfer_resume_and_deleted_bundle_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.db';queue=root/'queue.db';scratch=root/'scratch'
            with closing(sqlite3.connect(source)) as db:
                db.executescript("CREATE TABLE actors(did TEXT,pds TEXT,updated_at INTEGER);INSERT INTO actors VALUES('did:plc:a','',1);")
            prepare(source,queue)
            payloads=[gzip.compress(json.dumps(record()).encode()),gzip.compress(json.dumps(record('b','2026-10-02T00:00:00.000Z',type='plc_tombstone')).encode())]
            manifest=[{'name':f'{1668643200+i*604800}.jsonl.gz','size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for i,data in enumerate(payloads)]
            (root/manifest[0]['name']).write_bytes(payloads[0])
            handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=tmp)
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}'
                limit=sum(len(p)+1 for p in payloads)*2
                with self.assertRaises(urllib.error.HTTPError):run(queue,scratch,manifest,url,limit,1,10**8)
                self.assertFalse((scratch/manifest[0]['name']).exists())
                with closing(sqlite3.connect(queue)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM plc_bundles').fetchone(),(1,))
                    self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='plc_reconstruction_complete'").fetchone(),('false',))
                (root/manifest[0]['name']).unlink()
                (root/manifest[1]['name']).write_bytes(payloads[1])
                with self.assertRaises(ValueError):run(queue,scratch,manifest,url,limit,1,10**8)
                (scratch/(manifest[1]['name']+'.part')).unlink()
                result=run(queue,scratch,manifest,url,limit,1,10**8)
                self.assertFalse(result['apply_ready'])
                self.assertEqual(result['reserved_body_bytes'],len(payloads[0])+1+2*(len(payloads[1])+1))
                self.assertFalse(list(scratch.glob('*.gz')))
                with closing(sqlite3.connect(queue)) as db:
                    self.assertEqual(db.execute('SELECT status FROM plc_latest').fetchone(),('tombstoned',))
                server.shutdown()
                self.assertEqual(run(queue,scratch,manifest,url,limit,1,10**8),result)
                with self.assertRaises(ValueError):run(queue,scratch,manifest[:1],url,limit,1,10**8)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_reject_manifest_gaps_and_paths(self):
        for name in ['../1668643200.jsonl.gz','1669248000.jsonl.gz']:
            with self.assertRaises(ValueError):validate_manifest([{'name':name,'size':1,'sha256':'0'*64}])


if __name__=='__main__':unittest.main()

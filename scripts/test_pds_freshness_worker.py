import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pds_cloud_pilot import read_json
from pds_freshness_queue import Queue
from pds_freshness_worker import refresh_one, refresh_batch
from pds_repair_ledger import Ledger


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,updated_at INTEGER,handle TEXT)')
        self.db.execute("INSERT INTO actors VALUES('did:plc:test','https://old.test',1,'keep.test')")
        self.db.commit()
        self.queue = Queue(Path(self.temp.name)/'queue.db')
        self.ledger = Ledger(Path(self.temp.name)/'ledger.db')
        self.queue.discover(['did:plc:test'], 1)
        self.status = 200
        self.conflict = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if owner.conflict:
                    owner.db.execute("UPDATE actors SET pds='https://concurrent.test',updated_at=2")
                    owner.db.commit()
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(json.dumps({'id':self.path.removeprefix('/') if self.path.startswith('/did:') else 'did:plc:test','service':[{'id':'#atproto_pds','type':'AtprotoPersonalDataServer','serviceEndpoint':'https://new.test'}]}).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def execute(self, sql, params):
        rows = [list(r) for r in self.db.execute(sql,params).fetchall()]
        self.db.commit()
        return rows

    def run_one(self):
        claim = self.queue.claim()[0]
        return refresh_one(self.execute,self.ledger,self.queue,claim,lambda did: read_json('http://127.0.0.1:'+str(self.server.server_port)+'/did'))

    def test_authoritative_migration_preserves_handle(self):
        result = self.run_one()
        self.assertEqual(result['outcome'],'migrated')
        self.assertEqual(self.db.execute('SELECT pds,handle FROM actors').fetchone(),('https://new.test','keep.test'))
        self.assertEqual(self.queue.metrics()['verified_count'],1)

    def test_transient_authority_failure_preserves_host(self):
        self.status = 503
        self.assertEqual(self.run_one()['outcome'],'retry')
        self.assertEqual(self.db.execute('SELECT pds FROM actors').fetchone()[0],'https://old.test')
        self.assertEqual(self.queue.metrics()['verified_count'],0)

    def test_source_change_during_resolution_is_not_overwritten(self):
        self.conflict = True
        self.assertEqual(self.run_one()['outcome'],'conflict')
        self.assertEqual(self.db.execute('SELECT pds FROM actors').fetchone()[0],'https://concurrent.test')
        self.assertEqual(self.queue.metrics()['states'],{'pending':1})

    def test_batch_commits_once_and_lost_response_is_held(self):
        self.db.execute("INSERT INTO actors VALUES('did:plc:other','',1,'other.test')")
        self.db.commit()
        self.queue.discover(['did:plc:other'], 2)
        writes = []

        def lost_response(sql, params):
            rows = self.execute(sql, params)
            if sql.startswith('UPDATE'):
                writes.append(sql)
                raise OSError('committed response lost')
            return rows

        claims = self.queue.claim()
        with self.assertRaises(OSError):
            refresh_batch(lost_response, self.ledger, self.queue, claims,
                          lambda did: read_json('http://127.0.0.1:' + str(self.server.server_port) + '/' + did))
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM actors WHERE pds='https://new.test'").fetchone()[0], 2)
        self.assertEqual(self.queue.metrics()['states'], {'uncertain': 2})
        self.assertEqual(self.queue.claim(now=int(time.time()) + 86400), [])
        self.assertEqual({self.ledger.current(c['did'])['state'] for c in claims}, {'unknown'})


if __name__ == '__main__':
    unittest.main()

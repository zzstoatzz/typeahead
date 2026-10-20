import http.server
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from pds_checkpoint_download import download


class DownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        src = self.root / 'source.db'
        with sqlite3.connect(src) as db:
            db.execute('CREATE TABLE actors(did TEXT,pds TEXT,updated_at INTEGER,handle TEXT,quality_score REAL)')
        payload = src.read_bytes()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = b'{"current_generation":42}' if self.path == '/info' else payload
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = 'http://127.0.0.1:' + str(server.server_port)

    def run_download(self, **changes):
        args = dict(max_bytes=100000, bytes_per_second=1000000, admit=lambda: None)
        return download(self.url, 'disposable-test-token', self.root / 'checkpoint.db', **(args | changes))

    def test_actual_http_checkpoint_verified_and_not_replaced(self):
        result = self.run_download()
        self.assertEqual(result['generation'], 42)
        self.assertFalse(result['published'])
        self.assertEqual((self.root / 'checkpoint.db').read_bytes(), (self.root / 'source.db').read_bytes())
        with self.assertRaises(ValueError): self.run_download()

    def test_size_and_failed_admission_do_not_publish_partial(self):
        with self.assertRaisesRegex(ValueError, 'budget'): self.run_download(max_bytes=100)
        self.assertFalse((self.root / 'checkpoint.db').exists())

    def test_late_health_failure_retains_partial_without_marking_complete(self):
        calls = 0
        def admit():
            nonlocal calls
            calls += 1
            if calls == 3: raise RuntimeError('Ingestion unhealthy')
        with self.assertRaises(RuntimeError): self.run_download(admit=admit)
        self.assertFalse((self.root / 'checkpoint.db').exists())
        self.assertTrue((self.root / 'checkpoint.db.part').exists())


if __name__ == '__main__': unittest.main()

import http.server
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from pds_checkpoint_download import download, HealthNotReady, wait_for_admission


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

    def test_real_download_waits_for_health_before_publication(self):
        paused = threading.Event()
        recovered = threading.Event()
        calls = 0
        errors = []
        def check():
            nonlocal calls
            calls += 1
            if calls >= 3 and not recovered.is_set():
                paused.set()
                raise HealthNotReady('write age')
        def run():
            try:
                self.run_download(admit=lambda: wait_for_admission(check, timeout=1, interval=.01))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(paused.wait(2))
            self.assertTrue((self.root / 'checkpoint.db.part').exists())
            self.assertFalse((self.root / 'checkpoint.db').exists())
        finally:
            recovered.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((self.root / 'checkpoint.db').read_bytes(), (self.root / 'source.db').read_bytes())

    def test_failure_before_export_preserves_generation_and_refuses_retry(self):
        calls = 0
        def admit():
            nonlocal calls
            calls += 1
            if calls == 2: raise RuntimeError('Health stopped export')
        with self.assertRaises(RuntimeError): self.run_download(admit=admit)
        self.assertFalse((self.root / 'checkpoint.db.part').exists())
        self.assertEqual(json.loads((self.root / 'checkpoint.db.attempt.json').read_text())['generation'], 42)
        with self.assertRaisesRegex(ValueError, 'reconciliation'): self.run_download()

    def test_late_health_failure_retains_partial_without_marking_complete(self):
        calls = 0
        def admit():
            nonlocal calls
            calls += 1
            if calls == 3: raise RuntimeError('Ingestion unhealthy')
        with self.assertRaises(RuntimeError): self.run_download(admit=admit)
        self.assertFalse((self.root / 'checkpoint.db').exists())
        self.assertTrue((self.root / 'checkpoint.db.part').exists())
        receipt = json.loads((self.root / 'checkpoint.db.attempt.json').read_text())
        self.assertEqual(receipt['generation'], 42)
        self.assertEqual(receipt['max_bytes'], 100000)
        self.assertFalse(receipt['published'])
        self.assertNotIn('disposable-test-token', json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            self.run_download()


class AdmissionTests(unittest.TestCase):
    def test_requires_two_healthy_checks_after_pause(self):
        states = iter([False, True, False, True, True])
        seen = []
        def check():
            healthy = next(states)
            seen.append(healthy)
            if not healthy:
                raise HealthNotReady('write age')
        wait_for_admission(check, timeout=.3, interval=.01)
        self.assertEqual(seen, [False, True, False, True, True])

    def test_prolonged_failure_aborts_without_relaxing_gate(self):
        def check():
            raise HealthNotReady('write age')
        started = time.monotonic()
        with self.assertRaisesRegex(HealthNotReady, 'pause budget'):
            wait_for_admission(check, timeout=.04, interval=.01)
        self.assertLess(time.monotonic()-started, .5)

    def test_disk_or_stop_failure_is_not_retried(self):
        calls = []
        def check():
            calls.append(1)
            raise RuntimeError('disk reserve')
        with self.assertRaisesRegex(RuntimeError, 'disk reserve'):
            wait_for_admission(check)
        self.assertEqual(calls, [1])


if __name__ == '__main__': unittest.main()

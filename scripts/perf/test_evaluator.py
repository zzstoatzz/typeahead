import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'snapshot_compaction'))
from recovery_fixture import create
import qualify


@unittest.skipUnless(os.environ.get('TYPEAHEAD_EVAL_SERVER'), 'native evaluation executable required')
class EvaluatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='typeahead-perf-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        create(self.root / 'fixtures')
        self.snapshot = self.root / 'fixtures/builds/legacy/index.db'
        self.overlay = self.root / 'overlay.db'
        self.nonce = uuid.uuid4().hex
        self.input_id = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.process = None
        self.addCleanup(self.stop)

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)

    def start(self, wait=True, extra_env=None):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('TURSO_', 'INDEX_R2_', 'LOGFIRE_', 'PREFECT_'))}
        env.update(TYPEAHEAD_EVAL_SNAPSHOT=str(self.snapshot), LOCAL_DB_PATH=str(self.overlay),
                   TYPEAHEAD_EVAL_NONCE=self.nonce, TYPEAHEAD_EVAL_INPUT_ID=self.input_id,
                   PORT=str(port))
        env.update(extra_env or {})
        self.log = self.root / ('server-' + str(time.monotonic_ns()) + '.log')
        with self.log.open('w') as output:
            self.process = subprocess.Popen([os.environ['TYPEAHEAD_EVAL_SERVER']], env=env,
                                            stdout=output, stderr=output, start_new_session=True)
        self.base = f'http://127.0.0.1:{port}'
        if wait:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.fail(self.log.read_text())
                try:
                    qualify.verify_target(self.base, self.nonce, .1, self.input_id)
                    return
                except (OSError, ValueError):
                    time.sleep(.02)
            self.fail(self.log.read_text())

    def test_native_identity_reference_and_concurrent_response_equivalence(self):
        self.start()
        cases = [{'id': 'actor', 'path': '/search?q=a&limit=10', 'status': 200,
                  'min_actors': 1, 'first_handle': 'recovery.test'},
                 {'id': 'empty', 'path': '/search?q=zzzz&limit=10', 'status': 200}]
        reference = qualify.capture(self.base, cases, 1, rps=10)
        self.assertTrue(reference['passed'], reference)
        rows = qualify.measure(self.base, cases, reference, 4, 4, 10, 1, 3)
        self.assertTrue(all(row['correct'] for row in rows), rows)
        self.assertEqual(len(rows), 8)
        with self.assertRaises(ValueError):
            qualify.verify_target(self.base, self.nonce, 1, 'b' * 64)
        cases_path = self.root / 'cases.json'
        reference_path = self.root / 'reference.json'
        report_path = self.root / 'qualification.json'
        cases_path.write_text(json.dumps(cases))
        command = [sys.executable, qualify.__file__, '--url', self.base, '--nonce', self.nonce,
                   '--input-id', self.input_id, '--cases', str(cases_path), '--rps', '10',
                   '--rounds', '2', '--duration', '2', '--timeout', '1']
        result = subprocess.run(command + ['--output', str(reference_path)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ['--reference', str(reference_path), '--output', str(report_path)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(report_path.read_text())['summary']
        self.assertEqual(summary['failures'], 0)
        self.assertFalse(summary['enough_samples'])
        self.assertFalse(summary['passed'])

    def test_nonempty_overlay_cannot_forge_pds_readiness(self):
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute("INSERT INTO index_meta VALUES('pds_scope_policy','fixture-policy')")
        self.start()
        self.stop()
        with sqlite3.connect(self.overlay) as conn:
            conn.execute("DELETE FROM sync_meta WHERE key='pds_scope_policy'")
            conn.execute("INSERT INTO actor_overlay VALUES('did:fixture',x'00',0,200,0)")
        self.start(wait=False)
        try:
            exit_code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            result, body = qualify.request(self.base, '/__evaluation', .2)
            with sqlite3.connect(self.overlay) as conn:
                policy = conn.execute("SELECT value FROM sync_meta WHERE key='pds_scope_policy'").fetchone()
                count = conn.execute('SELECT count(*) FROM actor_overlay').fetchone()
            self.fail(self.log.read_text() + repr((result, body, policy, count)))
        self.assertNotEqual(exit_code, 0)
        self.assertIn('UnreadyEvaluationOverlay', self.log.read_text())
        with sqlite3.connect(self.overlay) as conn:
            self.assertIsNone(conn.execute("SELECT value FROM sync_meta WHERE key='pds_scope_policy'").fetchone())

    def test_private_capture_exports_real_packed_actor_and_refuses_overwrite(self):
        self.snapshot = self.root / 'fixtures/builds/compact/index.db'
        self.input_id = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        target = self.root / 'captured.json'
        env = {'TYPEAHEAD_EVAL_CAPTURE_FEED': str(target), 'TYPEAHEAD_EVAL_CAPTURE_COUNT': '1'}
        self.start(wait=False, extra_env=env)
        self.assertEqual(self.process.wait(timeout=10), 0, self.log.read_text())
        captured = json.loads(target.read_text())
        self.assertEqual(len(captured), 1)
        with sqlite3.connect(self.root / 'fixtures/builds/legacy/index.db') as conn:
            expected = conn.execute('SELECT blob FROM actor_compact_base WHERE did=?', (captured[0]['did'],)).fetchone()
        self.assertIsNotNone(expected)
        self.assertEqual(bytes.fromhex(captured[0]['blob_hex']), expected[0])
        before = target.read_bytes()
        self.start(wait=False, extra_env=env)
        self.assertNotEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(before, target.read_bytes())

    def test_cached_private_restart_preserves_frozen_overlay_without_prefill(self):
        self.snapshot = self.root / 'fixtures/builds/compact/index.db'
        self.input_id = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        with sqlite3.connect(self.root / 'fixtures/builds/legacy/index.db') as conn:
            did, blob = conn.execute('SELECT did,blob FROM actor_compact_base LIMIT 1').fetchone()
        feed = self.root / 'feed.json'
        feed.write_text(json.dumps([{'did': did, 'blob_hex': blob.hex()}]))
        writer = {'TYPEAHEAD_EVAL_FEED': str(feed), 'TYPEAHEAD_EVAL_WRITER_MODE': 'mixed',
                  'TYPEAHEAD_EVAL_WRITER_START': str(self.root / 'never-start')}
        self.start(extra_env=writer)
        cases = [{'id': 'actor', 'path': '/search?q=a&limit=10', 'status': 200,
                  'min_actors': 1, 'first_handle': 'recovery.test'}]
        reference = qualify.capture(self.base, cases, 1)
        self.assertTrue(reference['passed'], reference)
        self.stop()
        self.assertTrue(Path(str(self.snapshot) + '.prefix.admitted').is_file())
        with sqlite3.connect(self.overlay) as conn:
            conn.execute('UPDATE actor_overlay SET updated_at=updated_at+1000')
            before = conn.execute('SELECT did,blob,hidden,updated_at,source FROM actor_overlay ORDER BY did').fetchall()
        self.start(extra_env={**writer, 'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT': '1',
                              'TYPEAHEAD_EVAL_PRIVATE_ROOT': str(self.root), 'TYPEAHEAD_EVAL_SKIP_PREFILL': '1'})
        rows = qualify.measure(self.base, cases, reference, 2, 1, 10, 1, 1)
        self.assertTrue(all(row['correct'] for row in rows), rows)
        self.stop()
        with sqlite3.connect(self.overlay) as conn:
            after = conn.execute('SELECT did,blob,hidden,updated_at,source FROM actor_overlay ORDER BY did').fetchall()
        self.assertEqual(before, after)
        self.start(wait=False, extra_env={'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT': '1',
                                         'TYPEAHEAD_EVAL_PRIVATE_ROOT': str(self.root)[:-1]})
        self.assertNotEqual(self.process.wait(timeout=5), 0)
        self.assertIn('InvalidEvaluationPrivateRoot', self.log.read_text())

    def test_native_captured_projection_overlaps_reads_without_changing_results(self):
        with sqlite3.connect(self.snapshot) as conn:
            did, blob = conn.execute('SELECT did,blob FROM actor_compact_base LIMIT 1').fetchone()
        feed = self.root / 'feed.json'
        feed.write_text(json.dumps([{'did': did, 'blob_hex': blob.hex()},
                                    {'did': did + '-second', 'blob_hex': blob.hex()}]))
        trigger = self.root / 'writer-start'
        self.start(extra_env={'TYPEAHEAD_EVAL_FEED': str(feed), 'TYPEAHEAD_EVAL_WRITER_MODE': 'mixed',
                              'TYPEAHEAD_EVAL_WRITER_START': str(trigger), 'TYPEAHEAD_EVAL_WRITER_DELAY_MS': '10'})
        cases = [{'id': 'actor', 'path': '/search?q=a&limit=10', 'status': 200,
                  'min_actors': 1, 'first_handle': 'recovery.test'}]
        reference = qualify.capture(self.base, cases, 1)
        initial = qualify.verify_target(self.base, self.nonce, 1, self.input_id)
        trigger.touch()
        rows = qualify.measure(self.base, cases, reference, 8, 4, 10, 1, 3)
        final = qualify.verify_target(self.base, self.nonce, 1, self.input_id)
        self.assertTrue(all(row['correct'] for row in rows), rows)
        self.assertGreater(final['writer']['batches'], initial['writer']['batches'])
        self.assertGreater(final['writer']['changes'], initial['writer']['changes'])
        self.assertEqual(final['writer']['failures'], 0)


if __name__ == '__main__':
    unittest.main()

"""Supervisor failure-path checks. Run on Linux; timeout check takes 15 seconds."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux resource limits')
class SupervisorTests(unittest.TestCase):
    def run_child(self, code):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'fake'
            binary.write_text('#!/usr/bin/python3\n' + code)
            binary.chmod(0o700)
            for name in ('overlay.db', 'index.db'):
                (root / name).touch()
            (root / 'queries.json').write_text('["bs"]')
            result = subprocess.run([
                sys.executable, str(Path(__file__).with_name('compare.py')),
                '--binary', str(binary), '--overlay', str(root / 'overlay.db'),
                '--snapshot', str(root / 'index.db'), '--queries', str(root / 'queries.json'),
                '--output', str(root / 'result.json'),
            ], capture_output=True, text=True, timeout=20)
            return result.returncode, json.loads((root / 'result.json').read_text())[0]

    def test_valid_comparison_passes(self):
        code, result = self.run_child('print(\'{"equal": true, "baseline_us": 10, "candidate_us": 1, "actors": 0, "baseline_overlay_rows": 0, "candidate_overlay_rows": 0}\')\n')
        self.assertEqual(code, 0)
        self.assertTrue(result['passed'])

    def test_mismatch_fails(self):
        code, result = self.run_child('print(\'{"equal": false}\')\n')
        self.assertEqual(code, 1)
        self.assertFalse(result['passed'])

    def test_process_failure_cannot_report_success(self):
        code, result = self.run_child('print(\'{"equal": true}\')\nraise SystemExit(2)\n')
        self.assertEqual(code, 1)
        self.assertFalse(result['passed'])

    def test_malformed_output_fails(self):
        code, result = self.run_child('print("not json")\n')
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'invalid_output')

    def test_wall_timeout_kills_child(self):
        code, result = self.run_child('import time\ntime.sleep(60)\n')
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'wall_timeout')


if __name__ == '__main__':
    unittest.main()

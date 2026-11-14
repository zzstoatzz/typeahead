import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

from test_compare import backend


class CloudComparisonTests(unittest.TestCase):
    def test_success_and_failure_preserve_complete_evidence_and_remove_scratch(self):
        for wrong_build in [False,True]:
            with self.subTest(wrong_build=wrong_build), tempfile.TemporaryDirectory() as scratch, \
                    backend('a'*14,wrong_build=wrong_build) as (a,_), backend('b'*14) as (b,_):
                child = subprocess.run([sys.executable,str(Path(__file__).with_name('cloud-compare.py')),
                    '--baseline',a,'--candidate',b,'--baseline-machine','a'*14,
                    '--candidate-machine','b'*14,'--build-id','b1793850000-fixture',
                    '--test-loopback'],capture_output=True,text=True,timeout=65,
                    env=dict(os.environ,TMPDIR=scratch))
                self.assertEqual(child.returncode,1 if wrong_build else 0,child.stderr)
                chunks = [json.loads(line.removeprefix('TA_COMPARE_RESULT '))
                    for line in child.stdout.splitlines() if line.startswith('TA_COMPARE_RESULT ')]
                self.assertTrue(chunks,child.stdout)
                self.assertEqual([row['part'] for row in chunks],list(range(chunks[0]['total'])))
                raw = zlib.decompress(base64.b64decode(''.join(row['data'] for row in chunks)))
                self.assertEqual({row['sha256'] for row in chunks},{hashlib.sha256(raw).hexdigest()})
                report = json.loads(raw)
                self.assertEqual(report['passed'],not wrong_build)
                self.assertTrue(report['test_only'])
                self.assertEqual(len(report['samples']),1 if wrong_build else 78)
                self.assertIn('TA_COMPARE_EXIT '+str(child.returncode),child.stdout)
                self.assertEqual(list(Path(scratch).iterdir()),[])


if __name__=='__main__':unittest.main()

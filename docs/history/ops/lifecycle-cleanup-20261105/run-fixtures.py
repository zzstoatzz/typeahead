import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path('/qualification')
environment = dict(os.environ, TA_BINARY='/app/typeahead-tantivy',
    TA_SLOT_MODULE='/app/tantivy-slot.py', TA_INPUT_ROOT='/app/input',
    TA_SLOT_SERVE_TEST='1', TA_REPOSITORY=str(root/'typeahead'),
    TA_MPS_REPOSITORY=str(root/'mps'), PYTHONPATH=str(root/'mps/packages/mps/src'))
started = time.monotonic()
results = []
commands = [
    [sys.executable, '-m', 'unittest', 'discover', '-s',
        str(root/'typeahead/scripts/snapshot_refresh'), '-p', 'test_tantivy_*.py', '-v'],
    [sys.executable, str(root/'mps/tests/test_typeahead_checkpoint_build.py'), '-v'],
]
for command in commands:
    remaining = 300 - (time.monotonic() - started)
    if remaining <= 0:
        raise SystemExit('Fixture execution budget exhausted')
    child = subprocess.Popen(command, env=environment, start_new_session=True)
    try:
        code = child.wait(timeout=remaining)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)
    results.append(dict(command=command, returncode=code))
    if code:
        break
passed = len(results) == len(commands) and all(row['returncode'] == 0 for row in results)
print(json.dumps(dict(event='lifecycle_fixture_result', passed=passed,
    elapsed_s=time.monotonic()-started, results=results)), flush=True)
raise SystemExit(0 if passed else 1)

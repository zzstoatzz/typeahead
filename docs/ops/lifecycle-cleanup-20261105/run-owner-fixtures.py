import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET

started = time.monotonic()
def expired(*_):
    raise TimeoutError('Controller fixture execution budget expired')
signal.signal(signal.SIGALRM, expired)
signal.alarm(600)
root = Path('/qualification')
url = 'https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz'
expected = '2cf10babba653310606f8b49876cfb679928669e7ddaa1fb41fb00ce73e64f66'
with urllib.request.urlopen(urllib.request.Request(url,
        headers={'User-Agent':'typeahead-controller-qualification/1'}), timeout=30) as response:
    archive = response.read(32*1024**2+1)
if len(archive) > 32*1024**2 or hashlib.sha256(archive).hexdigest() != expected:
    raise SystemExit('uv archive identity or size rejected')
with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
    stream = bundle.extractfile('uv-x86_64-unknown-linux-gnu/uv')
    assert stream is not None
    executable = Path('/tmp/qualification-uv')
    executable.write_bytes(stream.read())
executable.chmod(0o755)
environment = dict(os.environ, PYTHONPATH=str(root/'mps/packages/mps/src'),
    TA_REPOSITORY=str(root/'typeahead'), TA_BINARY='/app/typeahead-tantivy',
    TA_MPS_REPOSITORY=str(root/'mps'), UV_CACHE_DIR='/tmp/qualification-uv-cache',
    UV_PYTHON_DOWNLOADS='never', UV_NO_CONFIG='1', UV_NO_PROGRESS='1',
    PREFECT_PLUGINS_ENABLED='false', PREFECT_SERVER_EPHEMERAL_ENABLED='false',
    PREFECT_SERVER_ANALYTICS_ENABLED='false',
    PREFECT_API_URL='http://127.0.0.1:9/api')
command = [str(executable), 'run', '--no-project', '--python', sys.executable,
    '--with', 'pytest==9.0.3', '--with', 'prefect==3.8.2',
    '--with', 'boto3==1.43.88', '--with', 'httpx==0.28.1',
    'python', '-m', 'pytest', '-q', '-ra',
    'tests/test_typeahead_engine_compatibility.py',
    'tests/test_typeahead_tantivy_slots.py',
    'tests/test_typeahead_tantivy_automatic.py',
    'tests/test_typeahead_index_retries.py', '--junitxml=/tmp/controller-owner.xml']
child = subprocess.Popen(command, cwd=root/'mps', env=environment, start_new_session=True)
try:
    code = child.wait(timeout=max(1,600-(time.monotonic()-started)))
finally:
    try:
        os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)
suites = ET.parse('/tmp/controller-owner.xml').getroot().findall('testsuite')
counts = {name:sum(int(s.get(name,'0')) for s in suites) for name in ['tests','failures','errors','skipped']}
passed = code==0 and counts['tests']>0 and all(counts[name]==0 for name in ['failures','errors','skipped'])
print(json.dumps(dict(event='controller_owner_fixture_result', passed=passed, counts=counts,
    elapsed_s=time.monotonic()-started, returncode=code)), flush=True)
raise SystemExit(0 if passed else 1)

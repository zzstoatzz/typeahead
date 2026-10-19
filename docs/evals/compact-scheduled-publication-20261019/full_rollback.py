import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.parse

os.umask(0o077)
root = Path('/home/stoat/typeahead-release-20261019/full-rollback')
root.mkdir(exist_ok=False)
cache = Path('/home/stoat/typeahead-release-20261019/cache')
new = Path('/home/stoat/typeahead-release-20261017/release-cancel-fix-v2')
old = Path('/home/stoat/typeahead-rollout-20261008/deployed-release-72')
assert hashlib.sha256(new.read_bytes()).hexdigest() == '758d458b829f350531364982d24136b9e1097f4e4c34977996a37488ea9a3606'
assert hashlib.sha256(old.read_bytes()).hexdigest() == 'f82400c65efa59456b3240828da41e496ad2e92532318972fbf9356e226ea920'
manifest = {v: json.loads((cache / (v + '.json')).read_text()) for v in ('baseline', 'compact')}
assert manifest['baseline']['source_watermark'] == manifest['compact']['source_watermark']
index = root / 'index'
index.mkdir()
for v, m in manifest.items():
    target = index / 'generations' / m['active_build']
    target.mkdir(parents=True)
    for source in (cache / 'generations' / m['active_build']).iterdir():
        if source.is_file():
            os.link(source, target / source.name)
(index / 'active.json').write_text(json.dumps({'version': 1,
    'current': {'manifest': manifest['compact'], 'location': 'generation'},
    'previous': {'manifest': manifest['baseline'], 'location': 'generation'}}))
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
env = dict(os.environ, MODE='search', INDEX_BUILD_ROOT=str(root), LOCAL_DB_PATH=str(root / 'overlay.db'),
           SQLITE_TMPDIR=str(root / 'tmp'), PORT=str(port), INDEX_POLL_SECS='86400',
           ADMIN_SECRET='local-compaction-evaluation', TURSO_URL='127.0.0.1:1', TURSO_AUTH_TOKEN='isolated-no-access')
process = None
results = {}

def request(path, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    try:
        connection.request('GET' if body is None else 'POST', path, body=None if body is None else json.dumps(body),
                           headers={'Authorization': 'Bearer local-compaction-evaluation', 'Content-Type': 'application/json'})
        response = connection.getresponse()
        content = response.read()
        assert response.status == 200, (response.status, content)
        return content
    finally:
        connection.close()

def start(stage, binary, remote):
    global process
    run_env = dict(env)
    if remote:
        run_env.update(INDEX_LATEST_KEY='staging/compact-scheduled-20261019/legacy.json')
        if binary == old:
            run_env['INDEX_R2_BUCKET'] = env['INDEX_R2_BUCKET'] + '/staging/compact-scheduled-20261019'
    else:
        run_env = {k: v for k, v in run_env.items() if not k.startswith('INDEX_R2_')}
    with (root / (stage + '.log')).open('w') as log:
        process = subprocess.Popen([str(binary)], env=run_env, stdout=log, stderr=log)
    wanted = manifest['baseline' if remote else 'compact']['active_build']
    started = time.monotonic()
    while time.monotonic() - started < 1800:
        assert process.poll() is None, (stage, process.returncode)
        try:
            state = json.loads(request('/index/status'))
            if state.get('build_id') == wanted:
                results[stage] = {'admission_seconds': time.monotonic() - started, 'build_id': wanted}
                return
        except (OSError, AssertionError):
            pass
        time.sleep(.1)
    raise RuntimeError((stage, 'admission timeout'))

def stop(old_binary=False):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGKILL if old_binary else signal.SIGTERM)
        process.wait(timeout=15)

queries = json.loads(Path('/home/stoat/typeahead-rollout-20261008/data/heldout3/queries.json').read_text())
assert isinstance(queries, list)
rows = json.loads(Path('/home/stoat/typeahead-rollout-20261008/data/heldout3/mutations.json').read_text())
queries = list(dict.fromkeys(queries + [row['handle'] for row in rows]))

def replies():
    return [request('/search?' + urllib.parse.urlencode({'q': q, 'limit': limit}))
            for q in queries for limit in (1, 10, 50, 100)]

try:
    start('compact', new, False)
    for row in rows:
        request('/admin/refresh', row)
        actors = json.loads(request('/search?' + urllib.parse.urlencode({'q': row['handle']})))['actors']
        assert (row['did'] in {actor['did'] for actor in actors}) == (not row['hidden'])
    expected = replies()
    stop()
    legacy = index / 'generations' / manifest['baseline']['active_build'] / 'index.db'
    os.link(legacy, index / 'index.A.db')
    fd = os.open(index, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)
    start('dual-reader-legacy', new, True)
    assert replies() == expected, 'format rollback changed responses'
    stop()
    start('deployed-reader-legacy', old, True)
    assert replies() == expected, 'binary rollback changed responses'
    stop(True)
    results['response_sha256'] = [hashlib.sha256(body).hexdigest() for body in expected]
    results['responses_per_stage'] = len(expected)
    results['mutations'] = len(rows)
    results['passed'] = True
    (root / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results), flush=True)
finally:
    stop(True)

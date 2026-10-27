import json
import socket
import signal
import subprocess
import time
import urllib.request
import uuid

image = 'registry.fly.io/typeahead-search-refresh@sha256:37fa2b5ed4cf21c63a1e75fefd0f48d001c2cf3d0635f5ee16441c40cece6525'
name = 'typeahead-rca-' + uuid.uuid4().hex[:10]
def interrupted(_signum, _frame):
    raise SystemExit('Interrupted')

signal.signal(signal.SIGTERM, interrupted)
connections = []
result = {'image': image, 'async_limit': 8, 'held_connections': 9, 'probe_deadline_s': 3, 'limits': {'cpus': 1, 'memory_mb': 256, 'scratch_mib': 16, 'read_only_root': True, 'pids': 64}, 'production_mutations': False}
try:
    subprocess.run(['docker', 'run', '-d', '--rm', '--pull=never', '--read-only', '--tmpfs', '/tmp:rw,size=16m', '--pids-limit', '64', '--name', name, '--platform', 'linux/amd64', '--memory', '256m', '--cpus', '1', '-p', '127.0.0.1::8080', '-e', 'MODE=search', '-e', 'LOCAL_DB_PATH=/tmp/overlay.db', '-e', 'INDEX_BUILD_ROOT=/tmp/rca', '--entrypoint', '/app/typeahead-ingester', image], check=True, capture_output=True, timeout=20)
    published = subprocess.check_output(['docker', 'port', name, '8080'], text=True, timeout=5).strip()
    port = int(published.rsplit(':', 1)[1])
    url = f'http://127.0.0.1:{port}/health'
    def probe(timeout):
        start = time.monotonic()
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.load(response)
            return {'http_status': response.status, 'elapsed_ms': round((time.monotonic()-start)*1000, 2), 'body': body}
    deadline = time.monotonic() + 10
    while True:
        try:
            result['baseline'] = probe(1)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    for _ in range(9):
        connection = socket.create_connection(('127.0.0.1', port), timeout=1)
        connection.sendall(b'GET /ready HTTP/1.1\r\nHost: localhost\r\n')
        connections.append(connection)
        time.sleep(0.05)
    start = time.monotonic()
    try:
        result['saturated'] = probe(3)
    except (OSError, TimeoutError) as error:
        result['saturated'] = {'error': type(error).__name__, 'elapsed_ms': round((time.monotonic()-start)*1000, 2)}
    for connection in connections:
        connection.sendall(b'Connection: close\r\n\r\n')
        connection.close()
    connections.clear()
    result['recovered'] = probe(3)
    result['reproduced'] = 'error' in result['saturated'] and result['recovered']['http_status'] == 200
    print(json.dumps(result, indent=2))
    if not result['reproduced']:
        raise SystemExit('Admission starvation was not reproduced')
finally:
    for connection in connections:
        connection.close()
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)

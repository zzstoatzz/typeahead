import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time


ROOT = Path('/data/refresh')


def object_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value):
        raise ValueError('Invalid artifact key')
    if any(p in ('.', '..') for p in value.split('/')):
        raise ValueError('Invalid artifact path')
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_manifest(m, expected):
    if m.get('engine', 'native') != 'native':
        raise ValueError('Native installer requires native artifacts; use tantivy_install.py for Tantivy directories')
    if not re.fullmatch(r'b[0-9]{10}-[A-Za-z0-9_-]+', expected) or m['active_build'] != expected:
        raise ValueError('Unexpected generation')
    if m['manifest_version'] != 2 or m['format_version'] != 5:
        raise ValueError('Unsupported generation')
    if not 0 <= time.time() - m['source_watermark'] < 3 * 86400:
        raise ValueError('Source watermark outside refresh window')
    for artifact in [m, m['prefix_map']]:
        if not isinstance(artifact['byte_size'], int) or artifact['byte_size'] <= 0 or not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
            raise ValueError('Invalid artifact identity')
    key = object_key(m['snapshot_url'])
    if not key.endswith('/' + expected + '/index.db') or object_key(m['prefix_map']['object_key']) != key + '.prefix':
        raise ValueError('Artifact generation mismatch')
    for key in ['actor_count', 'prefix_count']:
        if m[key] < int(os.environ.get('REFRESH_MIN_' + key.upper(), '1')):
            raise ValueError('Corpus count floor failed')


def validate_database(path, m):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Snapshot database is corrupt')
        meta = dict(db.execute('SELECT key,value FROM index_meta'))
        fields = ['source_watermark', 'actor_count', 'prefix_count', 'normalize_version', 'scoring_version', 'top_n_short', 'top_n_medium', 'top_n_long', 'overlay_top_n', 'hydrate_cap', 'cjk_key_count', 'cjk_actor_count']
        if meta['build_id'] != m['active_build'] or meta['flat_prefix_version'] != '5' or meta['flat_prefix_sha256'] != m['prefix_map']['sha256']:
            raise ValueError('Database identity mismatch')
        if any(meta[k] != str(m[k]) for k in fields):
            raise ValueError('Database metadata mismatch')
        if meta.get('pds_scope_version') != '1':
            raise ValueError('Snapshot lacks scoped search')


def install(config):
    prefix = ['rclone', '--config', str(config), '--contimeout', '10s', '--timeout', '30s', '--retries', '3', '--multi-thread-streams', '8', '--multi-thread-cutoff', '64M', '--multi-thread-chunk-size', '16M']
    bucket = os.environ['INDEX_R2_BUCKET']

    def remote(key):
        return 'r2:' + bucket + '/' + object_key(key)

    def run(args):
        result = subprocess.run(prefix + args, capture_output=True)
        if result.returncode:
            raise RuntimeError('Artifact transfer failed; exit=' + str(result.returncode))
        return result.stdout

    expected = os.environ['REFRESH_BUILD_ID']
    pointer = object_key(os.environ['INDEX_LATEST_KEY'])
    if not pointer.startswith('control/slots/'):
        raise ValueError('Installer requires an isolated slot pointer')
    raw = run(['cat', remote(pointer)])
    if len(raw) > 65536:
        raise ValueError('Manifest too large')
    m = json.loads(raw)
    validate_manifest(m, expected)
    generation = ROOT / 'index/generations' / expected
    receipt = generation / 'installation.json'
    identity = hashlib.sha256(raw).hexdigest()
    if receipt.exists() and json.loads(receipt.read_text()).get('manifest_sha256') == identity:
        if (generation / 'index.db.prefix.admitted').is_file():
            return m
    generation.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(ROOT).free < m['prefix_map']['byte_size'] + 2 * 1024**3:
        raise RuntimeError('Insufficient installation disk reserve')
    atomic_json(ROOT / 'install-status.json', {'phase': 'downloading', 'build_id': expected})
    for name, artifact, key in [('index.db', m, m['snapshot_url']), ('index.db.prefix', m['prefix_map'], m['prefix_map']['object_key'])]:
        path = generation / name
        part = path.with_suffix(path.suffix + '.download')
        print('Downloading ' + name + ' (' + str(artifact['byte_size']) + ' bytes)', flush=True)
        run(['copyto', remote(key), str(part)])
        if part.stat().st_size != artifact['byte_size']:
            part.unlink()
            raise ValueError('Downloaded artifact size mismatch')
        if name == 'index.db':
            with part.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != artifact['sha256']:
                    part.unlink()
                    raise ValueError('Snapshot checksum mismatch')
        with part.open('rb') as stream:
            os.fsync(stream.fileno())
        part.replace(path)
    validate_database(generation / 'index.db', m)
    atomic_json(ROOT / 'install-status.json', {'phase': 'admitting', 'build_id': expected})
    result = subprocess.run(['/app/admit-prefix'], env=dict(os.environ, TYPEAHEAD_ADMIT_PREFIX_PATH=str(generation / 'index.db.prefix'), TYPEAHEAD_ADMIT_PREFIX_SHA256=m['prefix_map']['sha256']))
    if result.returncode:
        (generation / 'index.db.prefix').unlink(missing_ok=True)
        raise RuntimeError('Full prefix verification/admission failed')
    atomic_json(generation / 'manifest.json', m)
    atomic_json(ROOT / 'index/active.json', {'version': 1, 'current': {'manifest': m, 'location': 'generation'}, 'previous': None})
    atomic_json(receipt, {'manifest_sha256': identity, 'build_id': expected, 'prefix_sha256': m['prefix_map']['sha256'], 'installed_at': int(time.time()), 'passed': True})
    atomic_json(ROOT / 'install-status.json', {'phase': 'installed', 'build_id': expected, 'passed': True})
    return m


def prepare():
    if os.environ.get('FLY_APP_NAME') not in ('typeahead-startup-20261024', 'typeahead-search-refresh'):
        raise RuntimeError('Installer is restricted to managed refresh slots')
    if not (os.environ.get('LOGFIRE_WRITE_TOKEN') or os.environ.get('LOGFIRE_TOKEN')):
        raise RuntimeError('Managed search requires a Logfire write token')
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', prefix='refresh-r2-', suffix='.conf') as config:
        config.write('[r2]\ntype = s3\nprovider = Cloudflare\nregion = auto\nno_check_bucket = true\n')
        for field, variable in [('endpoint', 'INDEX_R2_ENDPOINT'), ('access_key_id', 'INDEX_R2_ACCESS_KEY_ID'), ('secret_access_key', 'INDEX_R2_SECRET_ACCESS_KEY')]:
            value = os.environ[variable]
            if not value or any(c in value for c in '\r\n\0'):
                raise ValueError('Invalid transport configuration')
            config.write(field + ' = ' + value + '\n')
        config.flush()
        m = install(Path(config.name))
    os.environ['INDEX_BUILD_ROOT'] = str(ROOT)
    os.environ['LOCAL_DB_PATH'] = str(ROOT / 'overlay.db')
    os.environ['MODE'] = 'search'
    return m


def main():
    prepare()
    os.execv('/app/typeahead-ingester', ['/app/typeahead-ingester'])


if __name__ == '__main__':
    main()

"""Cloud-local immutable qualification preparation. Does not start a serving process.

QUAL_SOURCE_ACCESS is a temporary platform secret containing build_id/database/prefix
HTTPS URLs. Sources stay on the owned volume; stdout contains metadata only.

Run: python3 /app/full_prepare.py --arm native|tantivy \
  --root /data/qualification/<unique-run-arm> --deadline <epoch-seconds>
The root must not exist; its canonical parent must already exist. Preparation is
bounded to 60 minutes, 19 GiB payload, and a 1 GiB free-space reserve. The caller
retains an independent guest/reaper deadline. No working index, server, live
source cursor, or performance/source-readiness attestation is created.

A successful root/baseline is read-only and contains prepared.json, source/index.db
and source/index.db.prefix. Native adds overlay.db and the prefix admission cache;
Tantivy adds tantivy/ plus export/import metadata. A later controller must budget
and create its own writable benchmark copy; it must never serve/mutate baseline.
Failure removes preparing/ and records metadata-only failure.json. Reaper/controller
retire the complete owned volume when the qualification is finished.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request

OWNER = 'native-qualification-v1'
PROTECTED = {'8e3d79b70dd748', '801539a6551778', '7845636a3d0ee8', '840e95a2153308'}
GIB = 1024 ** 3

class PreparationError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PreparationError('source_redirect')

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()

def atomic(path, value):
    temporary = path.with_name(path.name + '.part')
    with temporary.open('xb') as stream:
        stream.write(encoded(value) + b'\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)

def manifest_check(value):
    if value.get('format_version') != 5 or not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', value.get('active_build', '')):
        raise PreparationError('manifest_identity')
    for item in [value, value.get('prefix_map', {})]:
        if not isinstance(item.get('byte_size'), int) or not 0 < item['byte_size'] <= 8 * GIB or not re.fullmatch('[0-9a-f]{64}', item.get('sha256', '')):
            raise PreparationError('manifest_digest')
    build = value['active_build']
    expected = f'releases/search/{build}/index.db'
    if value.get('snapshot_url', expected) != expected or value['prefix_map'].get('object_key') != expected + '.prefix':
        raise PreparationError('manifest_object_key')
    if not isinstance(value.get('actor_count'), int) or not 0 < value['actor_count'] <= 20_000_000 or not isinstance(value.get('source_watermark'), int):
        raise PreparationError('manifest_count')
    if value.get('normalize_version') != 'v2-cjk-bigram' or value.get('scoring_version') != 'v1-quality-score':
        raise PreparationError('manifest_versions')
    return value

class Preparation:
    def __init__(self, root, manifest, arm, deadline, *, app='/app', min_free=GIB,
                 max_bytes=19 * GIB, allow_loopback_fixture=False):
        self.root = Path(root).absolute(); self.manifest = manifest_check(manifest); self.arm = arm
        self.app = Path(app); self.min_free = min_free; self.max_bytes = max_bytes
        self.deadline = deadline; self.started = time.monotonic(); self.children = []
        self.fixture = allow_loopback_fixture
        if arm not in {'native', 'tantivy'} or not time.time() < deadline <= time.time() + 3600:
            raise PreparationError('preparation_bounds')
        if min_free < GIB or max_bytes > 19 * GIB or max_bytes <= 0:
            raise PreparationError('disk_bounds')
        if os.environ.get('FLY_MACHINE_ID') in PROTECTED:
            raise PreparationError('protected_machine')
        if not self.fixture:
            if not os.environ.get('FLY_APP_NAME', '').startswith('typeahead-qual-') or not str(self.root).startswith('/data/qualification/'):
                raise PreparationError('private_qualification_required')
        for path in [self.root, *self.root.parents]:
            if path.is_symlink(): raise PreparationError('scratch_symlink')
        if self.root.exists(): raise PreparationError('scratch_exists')
        if not self.root.parent.is_dir() or self.root.parent.resolve() != self.root.parent:
            raise PreparationError('scratch_parent')
        self.stage = self.root / 'preparing'; self.base = self.root / 'baseline'

    def guard(self):
        if time.time() >= self.deadline: raise PreparationError('preparation_deadline')
        if shutil.disk_usage(self.root if self.root.exists() else self.root.parent).free < self.min_free:
            raise PreparationError('disk_reserve')
        total = 0
        if self.stage.exists():
            for directory, dirs, files in os.walk(self.stage):
                for name in dirs + files:
                    path = Path(directory) / name
                    try: info = path.lstat()
                    except FileNotFoundError: continue
                    if stat.S_ISLNK(info.st_mode): raise PreparationError('scratch_symlink')
                    if stat.S_ISREG(info.st_mode): total += info.st_size
        if total > self.max_bytes: raise PreparationError('disk_payload_budget')

    def check_url(self, url, key):
        try: parsed = urllib.parse.urlsplit(url)
        except ValueError: raise PreparationError('source_url') from None
        local = self.fixture and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
        if parsed.username or parsed.password or parsed.fragment or not (local or (parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.r2.cloudflarestorage.com') and parsed.port in {None,443})):
            raise PreparationError('source_url')
        path = urllib.parse.unquote(parsed.path)
        if not path.endswith('/' + key) or any(part in {'.', '..'} for part in path.split('/')):
            raise PreparationError('source_object_binding')

    def download(self, url, destination, expected, key):
        self.check_url(url, key); self.guard()
        if shutil.disk_usage(self.root).free < expected['byte_size'] + self.min_free:
            raise PreparationError('download_disk_budget')
        part = destination.with_name(destination.name + '.part'); count = 0; digest = hashlib.sha256()
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(url, timeout=min(15, max(.1, self.deadline-time.time()))) as source, part.open('xb') as output:
                if source.status != 200: raise PreparationError('source_http_status')
                length = source.headers.get('Content-Length')
                if length is not None and int(length) != expected['byte_size']: raise PreparationError('source_size')
                while True:
                    self.guard()
                    chunk = source.read(1024 * 1024)
                    if not chunk: break
                    count += len(chunk)
                    if count > expected['byte_size']: raise PreparationError('source_size')
                    output.write(chunk); digest.update(chunk)
                output.flush(); os.fsync(output.fileno())
            if count != expected['byte_size']: raise PreparationError('source_size')
            if digest.hexdigest() != expected['sha256']: raise PreparationError('source_sha256')
            part.replace(destination)
        except PreparationError: raise
        except Exception: raise PreparationError('source_download_failed') from None
        finally: part.unlink(missing_ok=True)

    def spawn(self, command, *, stdin=None, stdout=None, env=None):
        # Capabilities never enter a subprocess environment or command line.
        safe_env = {key:value for key,value in os.environ.items() if key in {'PATH', 'HOME', 'LANG', 'FLY_APP_NAME'}}
        child = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
                                 env=safe_env | (env or {}), start_new_session=True)
        self.children.append(child); return child

    def wait(self, children):
        while any(child.poll() is None for child in children):
            self.guard()
            if any(child.poll() not in {None,0} for child in children): raise PreparationError('preparation_child_failed')
            time.sleep(.1)
        if any(child.returncode for child in children): raise PreparationError('preparation_child_failed')
        self.guard()

    def stop(self):
        for child in reversed(self.children):
            if child.poll() is None:
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    child.wait(timeout=2)
            if child.stdout is not None: child.stdout.close()

    def native(self, source):
        database = source / 'index.db'; prefix = source / 'index.db.prefix'
        database.chmod(0o444); prefix.chmod(0o444)
        child = self.spawn([str(self.app/'bin/admit-prefix')], stdout=subprocess.DEVNULL,
                           env={'TYPEAHEAD_ADMIT_PREFIX_PATH':str(prefix), 'TYPEAHEAD_ADMIT_PREFIX_SHA256':self.manifest['prefix_map']['sha256']})
        self.wait([child])
        (self.stage/'tmp').mkdir()
        child = self.spawn([str(self.app/'bin/qualification-native')], stdout=subprocess.DEVNULL, env={
            'NATIVE_QUAL_ROOT':str(self.root), 'TYPEAHEAD_EVAL_SNAPSHOT':str(database),
            'LOCAL_DB_PATH':str(self.stage/'overlay.db'), 'SQLITE_TMPDIR':str(self.stage/'tmp'),
            'NATIVE_QUAL_REQUEST':str(self.stage/'request.json'), 'NATIVE_QUAL_RESPONSE':str(self.stage/'response.json'),
            'NATIVE_QUAL_FEED_SHA256':'0'*64, 'NATIVE_QUAL_SECONDS':'1',
            'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1'})
        self.wait([child])
        with closing(sqlite3.connect(f'file:{self.stage}/overlay.db?mode=ro', uri=True)) as db:
            if db.execute('SELECT count(*) FROM actor_overlay').fetchone()[0] or db.execute('SELECT count(*) FROM prefix_overlay').fetchone()[0]:
                raise PreparationError('native_overlay_not_empty')
        (self.stage/'tmp').rmdir()
        return {'native_prefix_admitted':True, 'independent_empty_overlay':True}

    def tantivy(self, source):
        receipt = self.stage/'export-receipt.json'; metadata_path = self.stage/'import-metadata.json'
        producer = self.spawn([sys.executable, str(self.app/'input/export.py'), '--manifest', str(source/'manifest.json'),
            '--database', str(source/'index.db'), '--prefix', str(source/'index.db.prefix'), '--build-id', self.manifest['active_build'],
            '--scratch-root', str(self.root), '--output', '-', '--receipt', str(receipt),
            '--max-seconds', str(max(1,int(self.deadline-time.time())))], stdout=subprocess.PIPE)
        with metadata_path.open('xb') as metadata_output:
            consumer = self.spawn([str(self.app/'bin/typeahead-tantivy'), 'import', '--data', str(self.stage/'tantivy'),
                '--input', '-', '--build-id', self.manifest['active_build'], '--writer-memory-mb', '128', '--batch-size', '50000'],
                stdin=producer.stdout, stdout=metadata_output)
            producer.stdout.close(); self.wait([producer,consumer])
        if metadata_path.stat().st_size > 512*1024 or receipt.stat().st_size > 65536: raise PreparationError('metadata_budget')
        imported = json.loads(metadata_path.read_text()); exported = json.loads(receipt.read_text())
        validate_import(self.manifest, exported, imported)
        # Source remains available in the immutable baseline; no JSONL corpus file is written.
        return {'export':exported, 'import':imported, 'direct_stream':True}

    def run(self, access):
        self.root.mkdir(mode=0o700); (self.root/'owner').write_text(OWNER); self.stage.mkdir()
        source = self.stage/'source'; source.mkdir()
        try:
            self.guard()
            if access.get('build_id') != self.manifest['active_build']: raise PreparationError('source_release_binding')
            for field,name,item in [('database','index.db',self.manifest), ('prefix','index.db.prefix',self.manifest['prefix_map'])]:
                self.download(access[field], source/name, item, f'releases/search/{self.manifest["active_build"]}/{name}')
            atomic(source/'manifest.json', self.manifest)
            # DB identity is checked independently for both arms, before native preparation.
            with closing(sqlite3.connect(f'file:{source}/index.db?mode=ro&immutable=1', uri=True)) as db:
                metadata = dict(db.execute('SELECT key,value FROM index_meta'))
            for key,wanted in [('build_id',self.manifest['active_build']),('actor_count',str(self.manifest['actor_count'])),
                               ('source_watermark',str(self.manifest['source_watermark'])),('normalize_version',self.manifest['normalize_version']),
                               ('scoring_version',self.manifest['scoring_version'])]:
                if metadata.get(key) != wanted: raise PreparationError('database_release_binding')
            evidence = self.native(source) if self.arm == 'native' else self.tantivy(source)
            self.guard()
            result = {'schema_version':1, 'owner':OWNER, 'arm':self.arm, 'prepared':True,
                'build_id':self.manifest['active_build'], 'source_watermark':self.manifest['source_watermark'],
                'actor_count':self.manifest['actor_count'], 'source_database_sha256':self.manifest['sha256'],
                'source_prefix_sha256':self.manifest['prefix_map']['sha256'], 'evidence':evidence,
                'wall_seconds':time.monotonic()-self.started, 'free_bytes':shutil.disk_usage(self.root).free,
                'min_free_bytes':self.min_free, 'max_payload_bytes':self.max_bytes, 'baseline_path':str(self.base),
                'serving_started':False, 'performance_qualified':False, 'live_source_ready':False,
                'baseline_immutable':True, 'benchmark_working_copy_created':False}
            atomic(self.stage/'prepared.json',result)
            for directory,dirs,files in os.walk(self.stage, topdown=False):
                for name in files:
                    path = Path(directory)/name
                    if path.stat().st_mode & 0o777 != 0o444: path.chmod(0o444)
                Path(directory).chmod(0o555)
            self.stage.rename(self.base)
            return result
        except BaseException as error:
            self.stop()
            if self.stage.exists():
                for directory,dirs,files in os.walk(self.stage):
                    Path(directory).chmod(0o700)
                    for name in files: (Path(directory)/name).chmod(0o600)
                shutil.rmtree(self.stage)
            atomic(self.root/'failure.json', {'prepared':False, 'arm':self.arm,
                'error_class':str(error) if isinstance(error,PreparationError) else 'preparation_failed',
                'staged_payload_retired':True, 'children_terminal':all(child.poll() is not None for child in self.children),
                'free_bytes':shutil.disk_usage(self.root).free})
            raise
        finally: self.stop()

def validate_import(manifest, exported, imported):
    count = manifest['actor_count']; sha = exported.get('actors_sha256'); coverage = imported.get('expiry_coverage') or {}
    if exported.get('partial') is not False or exported.get('source_unchanged') is not True or exported.get('emitted_actor_count') != count or exported.get('source_actor_count') != count:
        raise PreparationError('export_incomplete')
    for key in ['source_prefix_sha256','source_database_sha256']:
        wanted = manifest['prefix_map']['sha256'] if key == 'source_prefix_sha256' else manifest['sha256']
        if exported.get(key) != wanted: raise PreparationError('export_source_digest')
    if not re.fullmatch('[0-9a-f]{64}',sha or '') or imported.get('build_id') != manifest['active_build'] or imported.get('snapshot_watermark') != manifest['source_watermark'] or imported.get('import_complete') is not True or imported.get('full_source') is not True or imported.get('input_actors') != count:
        raise PreparationError('import_incomplete')
    current = coverage.get('current') or {}
    if coverage.get('schema_version') != 1 or coverage.get('import_complete') is not True or coverage.get('full_source') is not True or coverage.get('expected_input_actors') != count or coverage.get('source_actor_count') != count or coverage.get('input_actors_checked') != count or coverage.get('verified_input_actors_sha256') != sha or current.get('live_actors') != imported.get('actor_count') or current.get('actors_with_unsupported_expiry') != 0 or current.get('actors_with_malformed_labels') != 0:
        raise PreparationError('expiry_coverage_incomplete')
    for key in ['canonical_rules_source_sha256','classifier_sha256']:
        if not re.fullmatch('[0-9a-f]{64}',coverage.get(key,'')): raise PreparationError('expiry_classifier_identity')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=['native','tantivy'], required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--deadline', type=float, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(Path('/app/source-manifest.json').read_text())
        access = json.loads(os.environ.pop('QUAL_SOURCE_ACCESS'))
        preparation = Preparation(args.root,manifest,args.arm,args.deadline)
        def interrupted(*_): raise PreparationError('preparation_interrupted')
        signal.signal(signal.SIGTERM,interrupted); signal.signal(signal.SIGINT,interrupted)
        signal.signal(signal.SIGALRM,interrupted); signal.setitimer(signal.ITIMER_REAL,max(.001,args.deadline-time.time()))
        result = preparation.run(access)
        print(json.dumps({key:result[key] for key in ['prepared','arm','build_id','actor_count','wall_seconds','free_bytes']}))
    except BaseException as error:
        code = str(error) if isinstance(error,PreparationError) else 'preparation_failed'
        print(json.dumps({'prepared':False,'error_class':code}),flush=True)
        raise SystemExit(1) from None

if __name__ == '__main__': main()

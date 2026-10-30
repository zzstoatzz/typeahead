"""Inactive-generation Tantivy directory admission; never installs native SQLite artifacts."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
from datetime import datetime

FORMAT = 'tantivy-0.27-f32-did-expiry-v4'
SOURCE_POLICY = 'turso-authoritative-observations-v2'
CHECKPOINT_SOURCE = 'sqlite-committed-prefix'
CHECKPOINT_POLICY = 'sqlite-checkpoint-observations-v1'
ORIGIN = 'https://typeahead-tantivy-dev.fly.dev'
SLOT_APPS = {'typeahead-tantivy-a', 'typeahead-tantivy-b'}
PUBLIC_APPS = SLOT_APPS | {'typeahead-tantivy-dev'}
BUILD = re.compile(r'b[0-9]{10}-[A-Za-z0-9_-]+')
DIGEST = re.compile(r'[a-f0-9]{64}')
PROTECTED_MACHINES = {'8e3d79b70dd748', '801539a6551778', '7845636a3d0ee8', '840e95a2153308'}
GUEST = {'cpu_kind':'shared', 'cpus':2, 'memory_mb':1024}


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Invalid ' + name)
    return value


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError('Invalid checksum')
    return value


def artifact_path(value):
    if value == 'pds-fallback.sqlite':return value
    if not isinstance(value, str) or not re.fullmatch(r'index/[A-Za-z0-9_.-]+', value) or value.split('/')[-1] in ('.', '..'):
        raise ValueError('Invalid Tantivy artifact path')
    if value.endswith('.lock') or value.endswith('.tmp'):
        raise ValueError('Live writer files are not immutable artifacts')
    return value


def fallback_identity(manifest):
    value=manifest.get('pds_fallback')
    if value is None:return None
    if not isinstance(value,dict) or set(value)!={'schema_version','path','sha256','byte_size','policy'} or value['schema_version']!=1 or value['path']!='pds-fallback.sqlite' or value['policy']!='empty-source-pds-only-v1':raise ValueError('Invalid PDS fallback identity')
    digest(value['sha256'])
    if not 4096<=integer(value['byte_size'],'fallback bytes')<=3*1024**3:raise ValueError('Fallback size outside bound')
    return value


def validate_fallback_file(data,manifest):
    value=fallback_identity(manifest)
    path=Path(data)/'pds-fallback.sqlite'
    if value is None:
        if path.exists():raise ValueError('Unbound PDS fallback file')
        return
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=value['byte_size']:raise ValueError('Required fallback file missing or differs')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=value['sha256']:raise ValueError('Fallback checksum differs')


def checkpoint_source(manifest):
    return manifest.get('source_kind') == CHECKPOINT_SOURCE


def embedded_checkpoint_receipt(manifest):
    bootstrap = manifest.get('source_bootstrap', {})
    raw = bootstrap.get('checkpoint_receipt_json')
    if not isinstance(raw, str) or len(raw.encode('utf8')) > 16384:
        raise ValueError('Exact bounded checkpoint receipt JSON required')
    raw = raw.encode('utf8')
    if hashlib.sha256(raw).hexdigest() != bootstrap.get('checkpoint_receipt_sha256') or json.loads(raw) != bootstrap.get('checkpoint_receipt'):
        raise ValueError('Embedded checkpoint receipt byte identity differs')
    return raw


def native_anchor_binding(manifest):
    if checkpoint_source(manifest):
        anchor = manifest['rollback_native_anchor']
        return anchor['build_id'], anchor['manifest_sha256']
    return manifest['source_build_id'], manifest['source_manifest_sha256']


def source_origin_sha256(value, allow_test_loopback=False):
    parsed = urllib.parse.urlsplit(value.replace('libsql://', 'https://'))
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/v2/pipeline') or (parsed.scheme == 'http' and not (allow_test_loopback and parsed.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ValueError('Invalid configured source origin')
    return hashlib.sha256(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', '')).encode()).hexdigest()


def validate_checkpoint_contract(manifest):
    if not checkpoint_source(manifest) or manifest.get('source_policy') != CHECKPOINT_POLICY:
        raise ValueError('Explicit checkpoint-only source identity required')
    if not isinstance(manifest.get('source_build_id'), str) or not BUILD.fullmatch(manifest['source_build_id']) or manifest.get('normalize_version') != 'v2-cjk-bigram':
        raise ValueError('Invalid primary checkpoint seed identity')
    bootstrap = manifest.get('source_bootstrap')
    if not isinstance(bootstrap, dict) or type(bootstrap.get('schema_version')) is not int or bootstrap.get('schema_version') != 1 or bootstrap.get('kind') != CHECKPOINT_SOURCE or bootstrap.get('coverage') != 'checkpoint-only' or bootstrap.get('live_coverage_verified') is not False:
        raise ValueError('Explicit unqualified checkpoint coverage required')
    allowed_bootstrap = {'schema_version','kind','coverage','live_coverage_verified','checkpoint_receipt_sha256','checkpoint_receipt','replay_floor','replay_overlap_seconds','actors_sha256','counts','fixture','native_visible_database_sha256','checkpoint_receipt_json','policy_freshness_attested','checkpoint_policy_sha256','checkpoint_policy_counts','raw_tombstone_rows'}
    if 'checkpoint_policy' in bootstrap:raise ValueError('Public checkpoint header must not expose full moderation policy')
    if set(bootstrap) - allowed_bootstrap:
        raise ValueError('Unknown public checkpoint bootstrap field')
    receipt = bootstrap.get('checkpoint_receipt')
    if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int or receipt.get('schema_version') != 1 or receipt.get('kind') != CHECKPOINT_SOURCE or type(receipt.get('fixture')) is not bool:
        raise ValueError('Checkpoint receipt identity missing')
    receipt_fields = {'schema_version','kind','fixture','generation','generation_after','source_origin_sha256','source_clock_before','source_clock_after','base_db_sha256','base_db_bytes','received_wal_frames','committed_frame','committed_provider_frames_sha256','replayed_wal_sha256','replayed_wal_bytes','database_sha256','database_bytes','sqlite_replay_verified','sqlite_verified_wal_frames','boundary_kind','received_source_bytes','source_requests','wall_seconds','live_coverage_verified','policy_freshness_attested'}
    if set(receipt) != receipt_fields:
        raise ValueError('Unknown or missing public checkpoint receipt field')
    if 'checkpoint_receipt_json' in bootstrap:
        embedded_checkpoint_receipt(manifest)
    if receipt.get('sqlite_replay_verified') is not True or receipt.get('boundary_kind') != 'successful-short-WAL-batch' or receipt.get('live_coverage_verified') is not False or receipt.get('policy_freshness_attested') is not False:
        raise ValueError('Verified checkpoint prefix is not a live-source proof')
    generation = integer(receipt.get('generation'), 'checkpoint generation')
    if integer(receipt.get('generation_after'), 'later checkpoint generation') < generation:
        raise ValueError('Checkpoint generation regressed')
    before = integer(receipt.get('source_clock_before'), 'checkpoint source clock', 1)
    if integer(receipt.get('source_clock_after'), 'later source clock', 1) < before:
        raise ValueError('Checkpoint source clock regressed')
    for key in ['source_origin_sha256', 'base_db_sha256', 'committed_provider_frames_sha256', 'replayed_wal_sha256', 'database_sha256']:
        digest(receipt.get(key))
    for key in ['base_db_bytes', 'database_bytes']:
        size = integer(receipt.get(key), key, 4096)
        if size % 4096 or size > 8 * 1024**3:
            raise ValueError('Checkpoint database byte bound differs')
    committed = integer(receipt.get('committed_frame'), 'committed WAL frame')
    received = integer(receipt.get('received_wal_frames'), 'received WAL frames')
    if received < committed or received * 4120 > 512 * 1024**2 or integer(receipt.get('sqlite_verified_wal_frames'), 'SQLite verified WAL frames') != committed or integer(receipt.get('replayed_wal_bytes'), 'replayed WAL bytes') != 32 + committed * 4120:
        raise ValueError('Committed WAL prefix accounting differs')
    integer(receipt.get('received_source_bytes'), 'received source bytes', receipt['base_db_bytes'] + received * 4120)
    integer(receipt.get('source_requests'), 'source request count', 1)
    elapsed = receipt.get('wall_seconds')
    if type(elapsed) not in (int, float) or not 0 <= elapsed <= 900:
        raise ValueError('Checkpoint wall-time bound differs')
    overlap = integer(bootstrap.get('replay_overlap_seconds'), 'replay overlap', 1)
    floor = integer(bootstrap.get('replay_floor'), 'replay floor', 1)
    if overlap > 3600 or floor != before - overlap or manifest.get('source_watermark') != floor:
        raise ValueError('Replay floor must precede the cut by its explicit overlap')
    counts = bootstrap.get('counts')
    names = {'raw_actor_rows', 'orphan_tombstones', 'input_documents', 'live_actor_documents', 'deleted_actor_documents', 'native_visible_actors'}
    if not isinstance(counts, dict) or set(counts) != names:
        raise ValueError('Explicit raw/input/live/native count contract required')
    for key in names - {'native_visible_actors'}:
        integer(counts[key], key)
    if counts['input_documents'] < 1 or counts['input_documents'] != counts['raw_actor_rows'] + counts['orphan_tombstones'] or counts['input_documents'] != counts['live_actor_documents'] + counts['deleted_actor_documents'] or counts['deleted_actor_documents'] < counts['orphan_tombstones'] or counts['live_actor_documents'] > counts['raw_actor_rows']:
        raise ValueError('Checkpoint input/live/deleted count equation differs')
    if counts['native_visible_actors'] is not None:
        native_count = integer(counts['native_visible_actors'], 'same-checkpoint native visible actors')
        if native_count > counts['live_actor_documents'] or bootstrap.get('native_visible_database_sha256') != receipt['database_sha256']:
            raise ValueError('Same-checkpoint visible count exceeds live actors')
    footer_hash = digest(manifest.get('source_actors_sha256'))
    if 'raw_tombstone_rows' in bootstrap:
        total_tombs = integer(bootstrap['raw_tombstone_rows'],'raw tombstone rows')
        if not counts['orphan_tombstones'] <= total_tombs <= counts['input_documents']:
            raise ValueError('Raw tombstone counts differ')
    if 'policy_freshness_attested' in bootstrap and bootstrap['policy_freshness_attested'] is not False:
        raise ValueError('Checkpoint policy cannot claim live freshness')
    if 'checkpoint_policy_sha256' in bootstrap:digest(bootstrap['checkpoint_policy_sha256'])
    if 'checkpoint_policy_counts' in bootstrap:
        policy_counts=bootstrap['checkpoint_policy_counts']
        if not isinstance(policy_counts,dict) or set(policy_counts) != {'overrides','blocked_domains'}:
            raise ValueError('Invalid safe checkpoint policy counts')
        for field in policy_counts:
            if integer(policy_counts[field],'checkpoint policy count')>10000:raise ValueError('Checkpoint policy exceeds cap')
    if manifest.get('source_actor_count') != counts['input_documents'] or ('actors_sha256' in bootstrap and digest(bootstrap['actors_sha256']) != footer_hash):
        raise ValueError('Checkpoint actor count/footer binding differs')
    if 'fixture' in bootstrap and bootstrap['fixture'] is not receipt['fixture']:
        raise ValueError('Checkpoint fixture identities differ')
    receipt_sha = digest(bootstrap.get('checkpoint_receipt_sha256'))
    if manifest.get('source_manifest_sha256') != receipt_sha or manifest.get('source_database_sha256') != receipt['database_sha256'] or 'source_prefix_sha256' in manifest:
        raise ValueError('Primary checkpoint provenance cannot be a native prefix source')
    anchor = manifest.get('rollback_native_anchor')
    if not isinstance(anchor, dict):
        raise ValueError('Independent rollback-native anchor required')
    for key in ['build_id', 'legacy_build_id']:
        if not isinstance(anchor.get(key), str) or not BUILD.fullmatch(anchor[key]):
            raise ValueError('Invalid rollback-native identity')
    for key in ['manifest_sha256', 'database_sha256', 'prefix_sha256']:
        digest(anchor.get(key))
    integer(anchor.get('actor_count'), 'rollback-native actor count', 1)
    integer(anchor.get('source_watermark'), 'rollback-native watermark', 1)
    if manifest.get('legacy_build_id') != anchor['legacy_build_id']:
        raise ValueError('Rollback-native twin binding differs')
    return bootstrap, anchor


def validate_checkpoint_provenance(manifest, checkpoint_receipt_bytes, native_anchor_manifest_bytes):
    bootstrap, anchor = validate_checkpoint_contract(manifest)
    if not isinstance(checkpoint_receipt_bytes, bytes) or len(checkpoint_receipt_bytes) > 1024 * 1024 or hashlib.sha256(checkpoint_receipt_bytes).hexdigest() != bootstrap['checkpoint_receipt_sha256'] or json.loads(checkpoint_receipt_bytes) != bootstrap['checkpoint_receipt']:
        raise ValueError('Exact primary checkpoint receipt bytes differ')
    if not isinstance(native_anchor_manifest_bytes, bytes) or len(native_anchor_manifest_bytes) > 1024 * 1024 or hashlib.sha256(native_anchor_manifest_bytes).hexdigest() != anchor['manifest_sha256']:
        raise ValueError('Exact rollback-native manifest bytes differ')
    native = json.loads(native_anchor_manifest_bytes)
    if not isinstance(native, dict):
        raise ValueError('Rollback-native manifest must be an object')
    integer(native.get('actor_count'), 'retained native actor count', 1)
    integer(native.get('source_watermark'), 'retained native watermark', 1)
    fields = [('active_build', 'build_id'), ('actor_count', 'actor_count'), ('source_watermark', 'source_watermark'), ('sha256', 'database_sha256')]
    if native.get('manifest_version') != 2 or native.get('format_version') != 5 or any(native.get(key) != anchor[field] for key, field in fields) or native.get('prefix_map', {}).get('sha256') != anchor['prefix_sha256'] or native.get('normalize_version') != manifest.get('normalize_version'):
        raise ValueError('Rollback-native provenance differs from its independent anchor')
    twin = native.get('legacy_twin', {})
    if twin.get('build_id') != anchor['legacy_build_id'] or twin.get('source_watermark') != anchor['source_watermark']:
        raise ValueError('Rollback-native twin provenance differs')
    return {'source_kind': CHECKPOINT_SOURCE, 'structurally_valid': True, 'coverage': 'checkpoint-only', 'live_admission': False, 'fixture': bootstrap['checkpoint_receipt']['fixture'], 'counts': dict(bootstrap['counts']), 'checkpoint_receipt_sha256': bootstrap['checkpoint_receipt_sha256'], 'rollback_manifest_sha256': anchor['manifest_sha256']}


def checkpoint_fixture_gate(manifest, allow_checkpoint_fixture=False):
    if manifest.get('source_kind') not in (None, 'flatpx05', CHECKPOINT_SOURCE):
        raise ValueError('Unsupported source kind')
    if not checkpoint_source(manifest):
        return False
    bootstrap, _ = validate_checkpoint_contract(manifest)
    embedded_checkpoint_receipt(manifest)
    fixture = bootstrap['checkpoint_receipt']['fixture'] or manifest.get('test_only') is True
    if fixture and not allow_checkpoint_fixture:
        raise ValueError('Fixture checkpoint is not eligible for production admission')
    return bool(fixture or allow_checkpoint_fixture)


def validate_artifact_manifest(manifest, expected):
    if not isinstance(expected, str) or not BUILD.fullmatch(expected) or manifest.get('active_build') != expected:
        raise ValueError('Unexpected Tantivy generation')
    if manifest.get('engine') != 'tantivy' or manifest.get('manifest_version') != 1 or manifest.get('artifact_format') != 'tantivy-directory-tar-v1' or manifest.get('backend_format') != FORMAT:
        raise ValueError('Unsupported Tantivy generation')
    if manifest.get('normalize_version') != 'v2-cjk-bigram' or manifest.get('plan_version') != 'complete-prefix-groups-v1':
        raise ValueError('Unsupported normalization contract')
    if checkpoint_source(manifest):
        validate_checkpoint_contract(manifest)
    elif manifest.get('source_kind') not in (None, 'flatpx05') or manifest.get('source_policy') != SOURCE_POLICY:
        raise ValueError('Unsupported source observation policy')
    for field in ['sha256', 'binary_sha256', 'canonical_rules_source_sha256']:
        digest(manifest.get(field))
    for field in ['source_manifest_sha256', 'source_database_sha256', 'source_actors_sha256'] + ([] if checkpoint_source(manifest) else ['source_prefix_sha256']):
        digest(manifest.get(field))
    for field in ['source_build_id', 'legacy_build_id']:
        if not isinstance(manifest.get(field), str) or not BUILD.fullmatch(manifest[field]):
            raise ValueError('Invalid native source identity')
    integer(manifest.get('source_watermark'), 'source watermark', 1)
    if manifest.get('retrieval_strategy') not in ('per-segment', 'global-merge'):
        raise ValueError('Unsupported retrieval strategy')
    integer(manifest.get('byte_size'), 'bundle bytes', 1)
    integer(manifest.get('source_actor_count'), 'full source count', 1)
    if manifest.get('object_key') != f'releases/search-tantivy/{expected}/index.tar':
        raise ValueError('Tantivy bundle generation mismatch')
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 100000:
        raise ValueError('Invalid file inventory')
    names = set()
    for item in files:
        path = artifact_path(item.get('path'))
        if path in names:
            raise ValueError('Duplicate artifact file')
        names.add(path)
        integer(item.get('byte_size'), 'file bytes')
        digest(item.get('sha256'))
    fallback=fallback_identity(manifest)
    if fallback:
        actual=next((r for r in files if r['path']==fallback['path']),None)
        if actual!={k:fallback[k] for k in ('path','byte_size','sha256')}:raise ValueError('Fallback inventory differs')
    elif 'pds-fallback.sqlite' in names:raise ValueError('Unbound fallback inventory')
    if 'index/meta.json' not in names:
        raise ValueError('Tantivy commit metadata missing')
    return manifest


def validate_source_manifest(manifest, source_bytes, *, checkpoint_receipt_bytes=None):
    validate_artifact_manifest(manifest, manifest['active_build'])
    if checkpoint_source(manifest):
        return validate_checkpoint_provenance(manifest, embedded_checkpoint_receipt(manifest) if checkpoint_receipt_bytes is None else checkpoint_receipt_bytes, source_bytes)
    if not isinstance(source_bytes, bytes) or len(source_bytes) > 1024 * 1024 or hashlib.sha256(source_bytes).hexdigest() != manifest['source_manifest_sha256']:
        raise ValueError('Native source manifest byte identity mismatch')
    source = json.loads(source_bytes)
    if source.get('manifest_version') != 2 or source.get('format_version') != 5:
        raise ValueError('Native source must be FLATPX05')
    pairs = [('active_build', 'source_build_id'), ('source_watermark', 'source_watermark'), ('actor_count', 'source_actor_count'), ('sha256', 'source_database_sha256'), ('normalize_version', 'normalize_version')]
    if any(source.get(key) != manifest[field] for key, field in pairs) or source.get('prefix_map', {}).get('sha256') != manifest['source_prefix_sha256']:
        raise ValueError('Native source manifest content mismatch')
    twin = source.get('legacy_twin', {})
    if twin.get('build_id') != manifest['legacy_build_id'] or twin.get('source_watermark') != manifest['source_watermark']:
        raise ValueError('Native rollback twin mismatch')
    return source


def validate_manifest(manifest, expected):
    validate_artifact_manifest(manifest, expected)
    if not isinstance(manifest.get('image_digest'), str) or not re.fullmatch(r'registry\.fly\.io/[a-z0-9-]+@sha256:[a-f0-9]{64}', manifest['image_digest']):
        raise ValueError('Immutable Fly image digest required')
    if manifest.get('guest') != GUEST or manifest.get('volume_size_gib') != 20 or not isinstance(manifest.get('volume_id'), str) or not re.fullmatch(r'vol_[A-Za-z0-9]+', manifest['volume_id']):
        raise ValueError('Final serving shape requires two shared CPUs, 1 GiB RAM and independent 20 GiB volume')
    if not isinstance(manifest.get('provider_app'), str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', manifest['provider_app']):
        raise ValueError('Expected provider app required')
    if manifest.get('public_service_ports') not in ([], [80, 443]):
        raise ValueError('Pinned provider ports must be isolated or the approved dev HTTPS interface')
    return manifest


def freeze_bundle(data, destination, manifest, source_bytes, binary, now=None, *, allow_checkpoint_fixture=False):
    """Freeze an owned, stopped checkpoint into a portable, source-linked artifact."""
    checkpoint_fixture_gate(manifest, allow_checkpoint_fixture)
    data, destination, binary = Path(data), Path(destination), Path(binary)
    if destination.exists() or (data / 'index').is_symlink():
        raise ValueError('A new artifact destination and regular index directory are required')
    with binary.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['binary_sha256']:
            raise ValueError('Publisher reader binary identity mismatch')
    result = subprocess.run([str(binary.resolve()), 'status', '--data', str(data), '--writer-memory-mb', '32'], capture_output=True, timeout=60)
    if result.returncode or len(result.stdout) > 8 * 1024**2:
        raise ValueError('Actual Tantivy reader rejected publisher checkpoint')
    metadata = json.loads(result.stdout)
    validate_source_manifest(manifest, source_bytes)
    validate_metadata(metadata, manifest, now, require_fresh=checkpoint_source(manifest), allow_checkpoint_fixture=allow_checkpoint_fixture)
    policy = metadata.get('policy_snapshot', {})
    observed = integer(policy.get('source_observed_at'), 'publisher policy observation', 1)
    if policy.get('complete') is not True or observed > (int(time.time()) if now is None else now):
        raise ValueError('Publisher policy checkpoint is incomplete or in the future')
    validate_fallback_file(data,manifest)
    def inventory():
        found = {}
        paths=list((data/'index').iterdir())
        if fallback_identity(manifest):paths.append(data/'pds-fallback.sqlite')
        for path in sorted(paths):
            if path.is_symlink() or not path.is_file():
                raise ValueError('Publisher checkpoint contains nonregular files')
            if path.name.endswith('.lock'):
                continue
            name = artifact_path(str(path.relative_to(data)))
            stat = path.stat()
            found[name] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return found
    before = inventory()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tantivy-publish-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive_path = staging / 'index.tar'
        files = []
        with tarfile.open(archive_path, 'w:') as archive:
            for name, witness in before.items():
                path = data / name
                checksum = hashlib.sha256()
                class Reader:
                    def read(self, size=-1):
                        chunk = stream.read(size)
                        checksum.update(chunk)
                        return chunk
                item = tarfile.TarInfo(name)
                item.size = witness[2]
                item.mode = 0o600
                with path.open('rb') as stream:
                    archive.addfile(item, Reader())
                files.append({'path': name, 'byte_size': item.size, 'sha256': checksum.hexdigest()})
        if inventory() != before:
            raise ValueError('Publisher checkpoint changed while freezing')
        portable = {key: value for key, value in manifest.items() if key not in {'image_digest', 'guest', 'volume_id', 'volume_size_gib', 'public_service_ports', 'provider_app'}}
        if allow_checkpoint_fixture:portable['test_only'] = True
        with archive_path.open('rb') as stream:
            portable.update(files=files, byte_size=archive_path.stat().st_size, sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
        validate_source_manifest(portable, source_bytes)
        validate_metadata(metadata, portable, now, require_fresh=checkpoint_source(manifest), allow_checkpoint_fixture=allow_checkpoint_fixture)
        (staging / 'manifest.json').write_text(json.dumps(portable, sort_keys=True, separators=(',', ':')) + '\n')
        for path in [archive_path, staging / 'manifest.json']:
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staging.rename(destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return portable


def source_identity(metadata, now):
    watermark = integer(metadata.get('watermark'), 'watermark')
    floor = integer(metadata.get('snapshot_watermark'), 'snapshot watermark')
    if not floor <= watermark <= now:
        raise ValueError('Source watermark identity is invalid')
    if integer(metadata.get('source_observation_revision'), 'source revision', 1) <= 0:
        raise ValueError('No authoritative source observations')
    cursor = metadata.get('source_cursor')
    if not isinstance(cursor, dict) or cursor.get('schema_version') != 1 or cursor.get('source_policy') != SOURCE_POLICY or cursor.get('build_id') != metadata.get('build_id') or cursor.get('snapshot_watermark') != floor or cursor.get('through') != watermark:
        raise ValueError('Source cursor identity/policy mismatch')
    if cursor.get('cycle') is not None and not isinstance(cursor['cycle'], dict):
        raise ValueError('Source cycle structure is invalid')
    return cursor, watermark


def source_gates(metadata, now, max_poll_age=120, max_policy_age=300, max_sweep_age=86400, max_sweep_duration=86400, require_fresh=True):
    for value, maximum in [(max_poll_age, 120), (max_policy_age, 300), (max_sweep_age, 86400), (max_sweep_duration, 86400)]:
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Admission bounds may only be tightened')
    cursor, watermark = source_identity(metadata, now)
    if require_fresh and now - watermark > max_poll_age:
        raise ValueError('Source polling is not fresh')
    if cursor.get('cycle') is not None:
        raise ValueError('Source discovery cycle is incomplete')
    reconciliation = cursor.get('reconciliation', {})
    if reconciliation.get('schema_version') != 1:
        raise ValueError('Reconciliation schema missing')
    end = integer(reconciliation.get('last_completed_at'), 'completed source sweep', 1)
    start = integer(reconciliation.get('last_completed_started_at'), 'completed sweep start', 1)
    if not start <= end <= now or (require_fresh and (now - end > max_sweep_age or end - start > max_sweep_duration)):
        raise ValueError('Full source sweep is missing, stale or too slow')
    return {'poll_age_s': now - watermark, 'sweep_age_s': now - end, 'sweep_duration_s': end - start}


def checkpoint_source_gates(metadata, manifest, now, max_poll_age=120, max_policy_age=300, max_sweep_age=86400, max_sweep_duration=86400, require_fresh=True, require_complete_poll=True):
    for value, maximum in [(max_poll_age,120),(max_policy_age,300),(max_sweep_age,86400),(max_sweep_duration,86400)]:
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Admission bounds may only be tightened')
    bootstrap, _ = validate_checkpoint_contract(manifest)
    receipt = bootstrap['checkpoint_receipt']
    cursor, watermark = source_identity(metadata, now)
    if require_complete_poll and (cursor.get('cycle') is not None or any(cursor.get(stream) != {column:watermark,'did':''} for stream,column in [('actors','updated_at'),('tombstones','deleted_at')])):
        raise ValueError('Checkpoint handoff requires an actually completed remote poll cycle')
    if integer(cursor.get('lookback_seconds'),'remote lookback') < bootstrap['replay_overlap_seconds']:
        raise ValueError('Remote replay overlap regressed')
    record = cursor.get('checkpoint_reconciliation')
    if not isinstance(record,dict) or type(record.get('schema_version')) is not int or record.get('schema_version') != 1 or record.get('kind') not in ('checkpoint-only','verified-checkpoint-import'):
        raise ValueError('Complete checkpoint-scoped reconciliation required')
    bootstrap_sha = digest_json(bootstrap)
    record_sha = digest_json(record)
    if digest(record.get('bootstrap_receipt_sha256')) != bootstrap_sha or digest(manifest.get('checkpoint_reconciliation_sha256')) != record_sha or digest(record.get('actors_sha256')) != manifest['source_actors_sha256']:
        raise ValueError('Checkpoint reconciliation immutable binding differs')
    counts = bootstrap['counts']
    if record['kind'] == 'checkpoint-only':
        if record.get('source_done') is not True or record.get('backend_done') is not True:
            raise ValueError('Both checkpoint-scoped identity walks must be complete')
        bindings = [('source_input_documents','input_documents'),('raw_actor_rows','raw_actor_rows'),('live_actor_documents','live_actor_documents'),('deleted_actor_documents','deleted_actor_documents'),('documents_checked','input_documents')]
    else:
        if any(key in record for key in ('source_done','backend_done','documents_checked')):
            raise ValueError('Verified import cannot claim unperformed identity walks')
        bindings = [('verified_input_actors','input_documents'),('raw_actor_rows','raw_actor_rows'),('initial_live_actor_documents','live_actor_documents'),('initial_deleted_actor_documents','deleted_actor_documents')]
    if any(type(record.get(key)) is not int or record[key] != counts[field] for key,field in bindings):
        raise ValueError('Checkpoint reconciliation document counts differ')
    started = integer(record.get('started_at'),'local checkpoint walk start',1)
    completed = integer(record.get('completed_at'),'local checkpoint walk completion',1)
    before,after = receipt['source_clock_before'],receipt['source_clock_after']
    if not before <= after <= completed <= now or not started <= completed or completed - started > max_sweep_duration or completed - before > max_sweep_duration:
        raise ValueError('Checkpoint export/import/reconciliation duration is invalid or too slow')
    handoff = cursor.get('checkpoint_handoff')
    if not isinstance(handoff,dict) or type(handoff.get('schema_version')) is not int or handoff.get('schema_version') != 1 or handoff.get('kind') != 'sqlite-checkpoint-to-turso-v2':
        raise ValueError('Explicit checkpoint-to-remote handoff required')
    digest(handoff.get('prior_cursor_sha256'))
    prior_revision = integer(handoff.get('prior_observation_revision'),'prior checkpoint observation revision')
    if integer(metadata.get('source_observation_revision'),'current observation revision',1) <= prior_revision:
        raise ValueError('Remote source observations did not advance past the checkpoint CAS')
    if handoff.get('checkpoint_reconciliation_sha256') != record_sha or handoff.get('bootstrap_receipt_sha256') != bootstrap_sha or handoff.get('source_origin_sha256') != receipt['source_origin_sha256'] or handoff.get('replay_floor') != manifest['source_watermark'] or digest(manifest.get('checkpoint_handoff_sha256')) != digest_json(handoff):
        raise ValueError('Checkpoint handoff lineage/source binding differs')
    first_through = integer(handoff.get('first_remote_through'),'first complete remote watermark',1)
    if not after <= first_through <= watermark:
        raise ValueError('Remote catch-up does not cover the verified checkpoint cut')
    policy = metadata.get('policy_snapshot')
    if isinstance(policy,dict):
        if policy.get('complete') is not True:raise ValueError('Complete post-cut remote policy required')
        if policy.get('schema_version') != 1 or not isinstance(policy.get('overrides'),list) or not isinstance(policy.get('blocked_domains'),list):raise ValueError('Complete post-cut policy shape required')
        policy_sha=digest_json({key:value for key,value in policy.items() if key!='source_observed_at'})
    else:
        policy = metadata.get('moderation',{})
        if policy.get('enabled') is not True or policy.get('complete') is not True or policy.get('canonical_rules_source_sha256') != manifest['canonical_rules_source_sha256']:
            raise ValueError('Complete canonical post-cut remote policy required')
        policy_sha=digest(policy.get('sha256'))
    observed_policy = integer(policy.get('source_observed_at'),'post-cut remote policy observation',1)
    if not after <= observed_policy <= now or (require_fresh and now-observed_policy > max_policy_age):
        raise ValueError('Remote policy is stale, future, or predates the verified checkpoint cut')
    anchor = before
    remote = cursor.get('reconciliation') or {}
    renewed = False
    if remote.get('last_completed_at') is not None or remote.get('last_completed_started_at') is not None:
        start = integer(remote.get('last_completed_started_at'),'remote full-round start',1)
        end = integer(remote.get('last_completed_at'),'remote full-round completion',1)
        if integer(remote.get('round'),'remote reconciliation round',2)<2 or any(not isinstance(remote.get(field),str) for field in ['source_after_did','backend_after_did']) or any(type(remote.get(field)) is not bool for field in ['source_done','backend_done']) or integer(remote.get('round_started_at'),'current remote round start',1)<end:
            raise ValueError('Canonical remote full-round state is incomplete')
        if remote.get('schema_version') != 1 or not first_through <= start <= end <= now or end-start > max_sweep_duration:
            raise ValueError('Checkpoint markers cannot be relabeled as a completed remote full round')
        anchor = max(anchor,end)
        renewed = end > before
    if require_fresh and (now-watermark > max_poll_age or now-anchor > max_sweep_age):
        raise ValueError('Checkpoint coverage or actual remote polling is stale')
    return {'poll_age_s':now-watermark,'source_coverage_kind':CHECKPOINT_SOURCE,'checkpoint_cut_age_s':now-before,
            'coverage_age_s':now-anchor,'coverage_deadline':anchor+max_sweep_age,'checkpoint_pipeline_duration_s':completed-before,
            'checkpoint_round_duration_s':completed-started,'checkpoint_reconciliation_sha256':record_sha,
            'checkpoint_handoff_sha256':digest_json(handoff),'checkpoint_receipt_sha256':bootstrap['checkpoint_receipt_sha256'],
            'post_cut_policy_sha256':policy_sha,'checkpoint_renewed_by_remote_round':renewed,'remote_full_round_present':remote.get('last_completed_at') is not None}


def checkpoint_coverage_deadline(metadata, manifest, now=None, allow_checkpoint_fixture=False):
    if not checkpoint_source(manifest):
        return None
    checkpoint_fixture_gate(manifest,allow_checkpoint_fixture)
    clock = int(time.time()) if now is None else now
    return checkpoint_source_gates(metadata,manifest,clock,require_fresh=False,require_complete_poll=False)['coverage_deadline']


def validate_metadata(metadata, manifest, now=None, require_coverage_ready=True, require_source_ready=True, allow_checkpoint_fixture=False, **bounds):
    checkpoint_test_only = checkpoint_fixture_gate(manifest, allow_checkpoint_fixture)
    now = int(time.time()) if now is None else now
    for field, expected in [('format', FORMAT), ('build_id', manifest['active_build']), ('normalize_version', manifest['normalize_version']), ('plan_version', manifest['plan_version'])]:
        if metadata.get(field) != expected:
            raise ValueError('Tantivy metadata identity mismatch: ' + field)
    if metadata.get('import_complete') is not True or metadata.get('full_source') is not True:
        raise ValueError('Tantivy full import is not admitted')
    source = metadata.get('source_manifest', {})
    if source.get('pds_fallback')!=fallback_identity(manifest):raise ValueError('Imported fallback provenance differs')
    count = manifest['source_actor_count']
    if source.get('partial') is not False or source.get('actor_count') != count or source.get('source_actor_count') != count or metadata.get('input_actors') != count:
        raise ValueError('Full source coverage count mismatch')
    integer(metadata.get('actor_count'), 'current actor count', 1)
    coverage = metadata.get('expiry_coverage')
    if not isinstance(coverage, dict) or coverage.get('schema_version') != 1 or coverage.get('import_complete') is not True or coverage.get('full_source') is not True:
        raise ValueError('Complete source-wide expiry coverage proof required')
    if coverage.get('canonical_rules_source_sha256') != manifest['canonical_rules_source_sha256'] or any(coverage.get(field) != count for field in ['expected_input_actors', 'source_actor_count', 'input_actors_checked']):
        raise ValueError('Expiry coverage source count or canonical rules mismatch')
    input_hash = digest(coverage.get('verified_input_actors_sha256'))
    if metadata.get('snapshot_watermark') != manifest['source_watermark'] or input_hash != manifest['source_actors_sha256'] or source.get('build_id') != manifest['source_build_id'] or source.get('source_watermark') != manifest['source_watermark'] or source.get('source_database_sha256') != manifest['source_database_sha256'] or (not checkpoint_source(manifest) and source.get('source_prefix_sha256') != manifest['source_prefix_sha256']):
        raise ValueError('Imported source differs from immutable artifact provenance')
    if checkpoint_source(manifest):
        if source.get('source_format') != 'sqlite-committed-prefix-v1' or source.get('source_kind') != CHECKPOINT_SOURCE or source.get('source_policy') != CHECKPOINT_POLICY or source.get('source_bootstrap') != manifest['source_bootstrap'] or source.get('rollback_native_anchor') != manifest['rollback_native_anchor'] or source.get('legacy_build_id') != manifest['legacy_build_id'] or 'source_prefix_sha256' in source:
            raise ValueError('Imported checkpoint header provenance differs')
    classifier_hash = digest(coverage.get('classifier_sha256'))
    current = coverage.get('current', {})
    if integer(current.get('live_actors'), 'expiry live actor count') != metadata['actor_count']:
        raise ValueError('Expiry coverage does not cover current live actors')
    unsupported = integer(current.get('actors_with_unsupported_expiry'), 'unsupported expiry actors')
    malformed = integer(current.get('actors_with_malformed_labels'), 'malformed label actors')
    if require_coverage_ready and (unsupported or malformed):
        raise ValueError('Unsupported or malformed source labels prevent expiry readiness')
    if not require_source_ready:source_identity(metadata, now)
    gates = checkpoint_source_gates(metadata,manifest,now,**dict(bounds,require_fresh=bounds.get('require_fresh',True) if require_source_ready else False,require_complete_poll=require_source_ready)) if checkpoint_source(manifest) else source_gates(metadata, now, **bounds) if require_source_ready else {}
    if checkpoint_source(manifest):gates['checkpoint_test_only'] = checkpoint_test_only
    gates.update(expiry_actors_checked=count, expiry_input_actors_sha256=input_hash, expiry_classifier_sha256=classifier_hash, expiry_current_live_actors=current['live_actors'], expiry_coverage_ready=not (unsupported or malformed))
    return gates


def validate_runtime(status, manifest, now=None, **bounds):
    if any(bounds.get(key,True) is not True for key in ['require_fresh','require_source_ready','require_coverage_ready','require_complete_poll']):raise ValueError('Runtime admission cannot disable freshness, coverage or completed-poll gates')
    now = int(time.time()) if now is None else now
    gates = validate_metadata(status, manifest, now, **bounds)
    if status.get('binary_sha256') != manifest['binary_sha256'] or status.get('retrieval_strategy') != manifest['retrieval_strategy']:
        raise ValueError('Live runtime binary or retrieval strategy mismatch')
    if status.get('engine') != 'tantivy' or status.get('ready') is not True or (not checkpoint_source(manifest) and status.get('cutover_source_ready') is not True) or status.get('pds_scope_version') != 1:
        raise ValueError('Tantivy runtime is not ready for admission')
    if checkpoint_source(manifest):
        config = status.get('query_config', {})
        if config.get('allow_partial') is not False or config.get('allow_stale') is not False or config.get('retrieval_strategy') != manifest['retrieval_strategy'] or type(config.get('max_sync_lag_s')) is not int or not 1 <= config['max_sync_lag_s'] <= 120:
            raise ValueError('Checkpoint serving requires strict non-fixture query settings')
        if type(status.get('cutover_source_ready')) is not bool or status['cutover_source_ready'] != gates['remote_full_round_present']:
            raise ValueError('Traditional reader full-round gate disagrees with explicit coverage kind')
    policy = status.get('moderation', {})
    policy_age = now - integer(policy.get('source_observed_at'), 'moderation observation', 1)
    if any(policy.get(key) is not True for key in ['enabled', 'complete', 'fresh']) or not 0 <= policy_age <= bounds.get('max_policy_age', 300):
        raise ValueError('Moderation policy is missing or stale')
    if checkpoint_source(manifest) and policy['source_observed_at'] < manifest['source_bootstrap']['checkpoint_receipt']['source_clock_after']:
        raise ValueError('Remote policy predates the verified checkpoint cut')
    digest(policy.get('sha256'))
    if policy.get('canonical_rules_source_sha256') != manifest['canonical_rules_source_sha256']:
        raise ValueError('Canonical moderation rules differ')
    gates['policy_age_s'] = policy_age
    return gates


def validate_provider(attestation, manifest, status, now=None):
    now = int(time.time()) if now is None else now
    if not isinstance(attestation, dict) or attestation.get('schema') != 1 or attestation.get('provider') != 'fly':
        raise ValueError('Normalized independent Fly API attestation required')
    try:
        stamp = datetime.fromisoformat(attestation['observed_at'].replace('Z', '+00:00'))
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError('Provider timestamp must be UTC')
        observed = stamp.timestamp()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('Invalid provider observation time') from None
    if not 0 <= now - observed <= 120:
        raise ValueError('Provider API observation is stale or in the future')
    role_owner = {'tantivy-control':'tantivy-qualification-20261029', 'dev-tantivy':'tantivy-dev-20261028', 'tantivy-slot':'typeahead-snapshot-refresh'}
    if attestation.get('owner') != role_owner.get(attestation.get('role')) or attestation.get('role') not in role_owner:
        raise ValueError('Provider ownership/role is not an isolated Tantivy candidate')
    machine = attestation.get('machine_id')
    if not isinstance(machine, str) or not re.fullmatch(r'[a-f0-9]{14}', machine) or machine in PROTECTED_MACHINES:
        raise ValueError('Protected or invalid provider machine')
    if not isinstance(attestation.get('instance_id'), str) or not attestation['instance_id'] or attestation.get('state') != 'started':
        raise ValueError('Provider instance is not started')
    ports = manifest['public_service_ports']
    if (attestation['role'] == 'tantivy-control' and ports != []) or (ports == [80, 443] and attestation['role'] not in {'dev-tantivy', 'tantivy-slot'}):
        raise ValueError('Qualification controls cannot expose serving ports')
    if attestation['role'] == 'tantivy-slot' and (manifest['provider_app'] not in SLOT_APPS or ports != [80, 443]):
        raise ValueError('Managed Tantivy slot app or serving ports mismatch')
    if attestation.get('app') != manifest['provider_app'] or attestation.get('image_digest') != manifest['image_digest'] or attestation.get('guest') != manifest['guest'] or attestation.get('public_service_ports') != ports or attestation.get('provider_config_reserved_env_overrides') is not False:
        raise ValueError('Provider image, app, guest or isolated ports mismatch')
    mounts = attestation.get('mounts')
    if mounts != [{'volume_id':manifest['volume_id'], 'path':'/data', 'size_gib':20, 'encrypted':True}]:
        raise ValueError('Provider volume is not the pinned encrypted independent 20 GiB mount')
    instance_bound = status.get('machine_id') == machine and status.get('instance_id') == attestation['instance_id']
    test_only = attestation.get('test_only') is True
    if not instance_bound and not test_only:
        raise ValueError('Live process does not match independently observed provider instance')
    return {'provider_config_verified':True, 'provider_runtime_instance_bound':instance_bound, 'provider_resources_qualified':instance_bound and not test_only, 'provider_attestation_sha256':digest_json(attestation), 'provider_observed_at':attestation['observed_at'], 'provider_machine_id':machine, 'provider_instance_id':attestation['instance_id'], 'provider_image_digest':attestation['image_digest'], 'provider_attestation_test_only':test_only}


def install_bundle(bundle, destination, manifest, binary, now=None, allow_checkpoint_fixture=False, **bounds):
    checkpoint_fixture_gate(manifest, allow_checkpoint_fixture)
    """Prepare one new inactive directory; caller owns starting and qualifying its service."""
    validate_manifest(manifest, manifest['active_build'])
    bundle, destination, binary = Path(bundle), Path(destination), Path(binary)
    if destination.exists():
        raise ValueError('Tantivy generation destination already exists')
    with bundle.open('rb') as stream:
        if bundle.stat().st_size != manifest['byte_size'] or hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('Tantivy bundle identity mismatch')
    with binary.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['binary_sha256']:
            raise ValueError('Tantivy reader binary identity mismatch')
    expected = {item['path']: item for item in manifest['files']}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < sum(item['byte_size'] for item in expected.values()) + 64 * 1024**2:
        raise ValueError('Insufficient Tantivy admission disk reserve')
    with tempfile.TemporaryDirectory(prefix='.tantivy-admit-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        seen = set()
        with tarfile.open(bundle, mode='r:') as archive:
            for member in archive:
                name = artifact_path(member.name)
                if not member.isfile() or name not in expected or name in seen or member.size != expected[name]['byte_size']:
                    raise ValueError('Tantivy archive inventory mismatch')
                seen.add(name)
                target = staging / name
                target.parent.mkdir(exist_ok=True)
                source = archive.extractfile(member)
                checksum = hashlib.sha256()
                with target.open('xb') as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError('Truncated Tantivy archive file')
                        checksum.update(chunk); output.write(chunk); remaining -= len(chunk)
                    output.flush(); os.fsync(output.fileno())
                if checksum.hexdigest() != expected[name]['sha256']:
                    raise ValueError('Tantivy file checksum mismatch')
        if seen != set(expected):
            raise ValueError('Tantivy bundle files missing')
        result = subprocess.run([str(binary.resolve()), 'status', '--data', str(staging), '--writer-memory-mb', '32'], capture_output=True, timeout=60)
        if result.returncode or len(result.stdout) > 8 * 1024**2:
            raise ValueError('Actual Tantivy reader rejected generation')
        metadata = json.loads(result.stdout)
        gates = validate_metadata(metadata, manifest, now, require_coverage_ready=False, require_fresh=False, allow_checkpoint_fixture=allow_checkpoint_fixture, **bounds)
        policy = metadata.get('policy_snapshot', {})
        observed = integer(policy.get('source_observed_at'), 'installed policy observation', 1)
        clock = int(time.time()) if now is None else now
        if policy.get('complete') is not True or clock < observed:
            raise ValueError('Installed policy is incomplete or stale')
        receipt = {'engine': 'tantivy', 'build_id': manifest['active_build'], 'format': FORMAT, 'manifest_sha256': digest_json(manifest), 'bundle_sha256': manifest['sha256'], 'binary_sha256': manifest['binary_sha256'], 'installed_at': clock, 'passed': True, 'runtime_qualified': False, 'test_only': bool(allow_checkpoint_fixture), **gates}
        with (staging / 'installation.json').open('x') as output:
            output.write(json.dumps(receipt, sort_keys=True) + '\n'); output.flush(); os.fsync(output.fileno())
        destination.mkdir()
        validate_fallback_file(staging,manifest)
        (staging / 'index').rename(destination / 'index')
        if fallback_identity(manifest):(staging/'pds-fallback.sqlite').rename(destination/'pds-fallback.sqlite')
        (staging / 'installation.json').rename(destination / 'installation.json')
        for path in [destination / 'index', destination, destination.parent]:
            descriptor = os.open(path, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    return receipt


def qualify_route(status, manifest, installation, now=None, observed_origin=None, provider_attestation=None, **bounds):
    validate_manifest(manifest, manifest['active_build'])
    if 'allow_checkpoint_fixture' in bounds:raise ValueError('Checkpoint fixture admission must be explicitly provider-test-only')
    fixture_mode = bool(provider_attestation and provider_attestation.get('test_only') is True and observed_origin and urllib.parse.urlsplit(observed_origin).hostname in ('127.0.0.1','localhost','::1'))
    gates = validate_runtime(status, manifest, now, allow_checkpoint_fixture=fixture_mode, **bounds)
    provider = validate_provider(provider_attestation, manifest, status, now)
    app = manifest['provider_app']
    if app not in PUBLIC_APPS and not provider['provider_attestation_test_only']:
        raise ValueError('Private qualification app cannot become a serving route')
    origin = f'https://{app}.fly.dev' if app in PUBLIC_APPS else ORIGIN
    if observed_origin is not None and observed_origin != origin and not urllib.parse.urlsplit(observed_origin).hostname in ('127.0.0.1', 'localhost'):
        raise ValueError('Observed origin differs from bound provider app')
    if installation.get('passed') is not True or installation.get('manifest_sha256') != digest_json(manifest) or installation.get('binary_sha256') != manifest['binary_sha256'] or installation.get('bundle_sha256') != manifest['sha256'] or installation.get('build_id') != manifest['active_build']:
        raise ValueError('Missing matching immutable installation receipt')
    receipt = {'schema_version': 1, 'engine': 'tantivy', 'build_id': manifest['active_build'], 'origin': origin, 'format': FORMAT, 'manifest_sha256': digest_json(manifest), 'installed_binary_sha256': installation['binary_sha256'], 'qualified_at': int(time.time()) if now is None else now, 'source_cursor_sha256': digest_json(status['source_cursor']), 'policy_sha256': status['moderation']['sha256'], 'slo_empirically_qualified': False, 'runtime_http_verified': observed_origin == origin, 'runtime_observed_origin': observed_origin, 'test_only': observed_origin is not None and observed_origin != origin, **gates}
    receipt.update(provider)
    receipt['test_only'] = receipt['test_only'] or provider['provider_attestation_test_only'] or gates.get('checkpoint_test_only',False)
    route = {'engine': 'tantivy', 'origin': origin, 'build_id': manifest['active_build'], 'format': FORMAT, 'admission_sha256': digest_json(receipt), 'binary_sha256':manifest['binary_sha256'], 'retrieval_strategy':manifest['retrieval_strategy']}
    return {'route': route, 'admission': receipt}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Admission HTTP redirects are prohibited')


def qualify_runtime(url, manifest, installation, allow_loopback=False, provider_attestation=None, **bounds):
    parsed = urllib.parse.urlsplit(url)
    loopback = allow_loopback and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
    expected = f"https://{manifest['provider_app']}.fly.dev" if manifest['provider_app'] in PUBLIC_APPS else None
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or not (url.rstrip('/') == expected or loopback):
        raise ValueError('Admission requires the known isolated Tantivy origin')
    opener = urllib.request.build_opener(NoRedirect())
    values = []
    for path in ['/index/status', '/ready']:
        with opener.open(urllib.request.Request(url.rstrip('/') + path, headers={'Accept':'application/json'}), timeout=5) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Admission status response is too large')
            if response.headers.get('X-Typeahead-Engine') != 'tantivy' or response.headers.get('X-Typeahead-Build') != manifest['active_build'] or response.headers.get('X-Typeahead-Format') != FORMAT:
                raise ValueError('Live backend identity acknowledgement mismatch')
            if response.headers.get('X-Typeahead-Binary-Sha256') != manifest['binary_sha256'] or response.headers.get('X-Typeahead-Retrieval-Strategy') != manifest['retrieval_strategy']:
                raise ValueError('Live runtime binary/strategy acknowledgement mismatch')
            if provider_attestation and provider_attestation.get('test_only') is not True and (response.headers.get('X-Typeahead-Machine-Id') != provider_attestation.get('machine_id') or response.headers.get('X-Typeahead-Machine-Version') != provider_attestation.get('instance_id')):
                raise ValueError('Live runtime machine/instance acknowledgement mismatch')
            values.append(json.loads(raw))
    if values[1].get('ready') is not True:
        raise ValueError('Live readiness gate failed')
    return qualify_route(values[0], manifest, installation, observed_origin=url.rstrip('/'), provider_attestation=provider_attestation, **bounds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['freeze', 'install', 'qualify'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--bundle')
    parser.add_argument('--destination')
    parser.add_argument('--binary')
    parser.add_argument('--data')
    parser.add_argument('--source-manifest')
    parser.add_argument('--installation')
    parser.add_argument('--provider-attestation')
    parser.add_argument('--url', default=ORIGIN)
    parser.add_argument('--allow-loopback', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    if args.operation == 'freeze':
        if not all([args.data, args.destination, args.binary, args.source_manifest]):
            parser.error('freeze requires --data, --destination, --binary and --source-manifest')
        result = freeze_bundle(args.data, args.destination, manifest, Path(args.source_manifest).read_bytes(), args.binary)
    elif args.operation == 'install':
        if not all([args.bundle, args.destination, args.binary]):
            parser.error('install requires --bundle, --destination and --binary')
        result = install_bundle(args.bundle, args.destination, manifest, args.binary)
    else:
        if not args.installation or not args.provider_attestation:
            parser.error('qualify requires --installation and --provider-attestation')
        result = qualify_runtime(args.url, manifest, json.loads(Path(args.installation).read_text()), args.allow_loopback, json.loads(Path(args.provider_attestation).read_text()))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()

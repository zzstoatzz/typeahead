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
    if not isinstance(value, str) or not re.fullmatch(r'index/[A-Za-z0-9_.-]+', value) or value.split('/')[-1] in ('.', '..'):
        raise ValueError('Invalid Tantivy artifact path')
    if value.endswith('.lock') or value.endswith('.tmp'):
        raise ValueError('Live writer files are not immutable artifacts')
    return value


def validate_artifact_manifest(manifest, expected):
    if not isinstance(expected, str) or not BUILD.fullmatch(expected) or manifest.get('active_build') != expected:
        raise ValueError('Unexpected Tantivy generation')
    if manifest.get('engine') != 'tantivy' or manifest.get('manifest_version') != 1 or manifest.get('artifact_format') != 'tantivy-directory-tar-v1' or manifest.get('backend_format') != FORMAT:
        raise ValueError('Unsupported Tantivy generation')
    if manifest.get('normalize_version') != 'v2-cjk-bigram' or manifest.get('plan_version') != 'complete-prefix-groups-v1':
        raise ValueError('Unsupported normalization contract')
    if manifest.get('source_policy') != SOURCE_POLICY:
        raise ValueError('Unsupported source observation policy')
    for field in ['sha256', 'binary_sha256', 'canonical_rules_source_sha256']:
        digest(manifest.get(field))
    for field in ['source_manifest_sha256', 'source_database_sha256', 'source_prefix_sha256', 'source_actors_sha256']:
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
    if 'index/meta.json' not in names:
        raise ValueError('Tantivy commit metadata missing')
    return manifest


def validate_source_manifest(manifest, source_bytes):
    validate_artifact_manifest(manifest, manifest['active_build'])
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


def freeze_bundle(data, destination, manifest, source_bytes, binary, now=None):
    """Freeze an owned, stopped checkpoint into a portable, source-linked artifact."""
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
    validate_metadata(metadata, manifest, now, require_fresh=False)
    policy = metadata.get('policy_snapshot', {})
    observed = integer(policy.get('source_observed_at'), 'publisher policy observation', 1)
    if policy.get('complete') is not True or observed > (int(time.time()) if now is None else now):
        raise ValueError('Publisher policy checkpoint is incomplete or in the future')
    def inventory():
        found = {}
        for path in sorted((data / 'index').iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError('Publisher checkpoint contains nonregular files')
            if path.name.endswith('.lock'):
                continue
            name = artifact_path('index/' + path.name)
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
        with archive_path.open('rb') as stream:
            portable.update(files=files, byte_size=archive_path.stat().st_size, sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
        validate_source_manifest(portable, source_bytes)
        validate_metadata(metadata, portable, now, require_fresh=False)
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


def source_gates(metadata, now, max_poll_age=120, max_policy_age=300, max_sweep_age=86400, max_sweep_duration=86400, require_fresh=True):
    for value, maximum in [(max_poll_age, 120), (max_policy_age, 300), (max_sweep_age, 86400), (max_sweep_duration, 86400)]:
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Admission bounds may only be tightened')
    watermark = integer(metadata.get('watermark'), 'watermark')
    floor = integer(metadata.get('snapshot_watermark'), 'snapshot watermark')
    if not floor <= watermark <= now or (require_fresh and now - watermark > max_poll_age):
        raise ValueError('Source polling is not fresh')
    if integer(metadata.get('source_observation_revision'), 'source revision', 1) <= 0:
        raise ValueError('No authoritative source observations')
    cursor = metadata.get('source_cursor')
    if not isinstance(cursor, dict) or cursor.get('schema_version') != 1 or cursor.get('source_policy') != SOURCE_POLICY or cursor.get('build_id') != metadata.get('build_id') or cursor.get('snapshot_watermark') != floor:
        raise ValueError('Source cursor identity/policy mismatch')
    if cursor.get('cycle') is not None or cursor.get('through') != watermark:
        raise ValueError('Source discovery cycle is incomplete')
    reconciliation = cursor.get('reconciliation', {})
    if reconciliation.get('schema_version') != 1:
        raise ValueError('Reconciliation schema missing')
    end = integer(reconciliation.get('last_completed_at'), 'completed source sweep', 1)
    start = integer(reconciliation.get('last_completed_started_at'), 'completed sweep start', 1)
    if not start <= end <= now or (require_fresh and (now - end > max_sweep_age or end - start > max_sweep_duration)):
        raise ValueError('Full source sweep is missing, stale or too slow')
    return {'poll_age_s': now - watermark, 'sweep_age_s': now - end, 'sweep_duration_s': end - start}


def validate_metadata(metadata, manifest, now=None, require_coverage_ready=True, **bounds):
    now = int(time.time()) if now is None else now
    for field, expected in [('format', FORMAT), ('build_id', manifest['active_build']), ('normalize_version', manifest['normalize_version']), ('plan_version', manifest['plan_version'])]:
        if metadata.get(field) != expected:
            raise ValueError('Tantivy metadata identity mismatch: ' + field)
    if metadata.get('import_complete') is not True or metadata.get('full_source') is not True:
        raise ValueError('Tantivy full import is not admitted')
    source = metadata.get('source_manifest', {})
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
    if input_hash != manifest['source_actors_sha256'] or source.get('build_id') != manifest['source_build_id'] or source.get('source_watermark') != manifest['source_watermark'] or source.get('source_database_sha256') != manifest['source_database_sha256'] or source.get('source_prefix_sha256') != manifest['source_prefix_sha256']:
        raise ValueError('Imported source differs from immutable artifact provenance')
    classifier_hash = digest(coverage.get('classifier_sha256'))
    current = coverage.get('current', {})
    if integer(current.get('live_actors'), 'expiry live actor count') != metadata['actor_count']:
        raise ValueError('Expiry coverage does not cover current live actors')
    unsupported = integer(current.get('actors_with_unsupported_expiry'), 'unsupported expiry actors')
    malformed = integer(current.get('actors_with_malformed_labels'), 'malformed label actors')
    if require_coverage_ready and (unsupported or malformed):
        raise ValueError('Unsupported or malformed source labels prevent expiry readiness')
    gates = source_gates(metadata, now, **bounds)
    gates.update(expiry_actors_checked=count, expiry_input_actors_sha256=input_hash, expiry_classifier_sha256=classifier_hash, expiry_current_live_actors=current['live_actors'], expiry_coverage_ready=not (unsupported or malformed))
    return gates


def validate_runtime(status, manifest, now=None, **bounds):
    now = int(time.time()) if now is None else now
    gates = validate_metadata(status, manifest, now, **bounds)
    if status.get('binary_sha256') != manifest['binary_sha256'] or status.get('retrieval_strategy') != manifest['retrieval_strategy']:
        raise ValueError('Live runtime binary or retrieval strategy mismatch')
    if status.get('engine') != 'tantivy' or status.get('ready') is not True or status.get('cutover_source_ready') is not True or status.get('pds_scope_version') != 1:
        raise ValueError('Tantivy runtime is not ready for admission')
    policy = status.get('moderation', {})
    policy_age = now - integer(policy.get('source_observed_at'), 'moderation observation', 1)
    if any(policy.get(key) is not True for key in ['enabled', 'complete', 'fresh']) or not 0 <= policy_age <= bounds.get('max_policy_age', 300):
        raise ValueError('Moderation policy is missing or stale')
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


def install_bundle(bundle, destination, manifest, binary, now=None, **bounds):
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
        gates = validate_metadata(metadata, manifest, now, require_coverage_ready=False, require_fresh=False, **bounds)
        policy = metadata.get('policy_snapshot', {})
        observed = integer(policy.get('source_observed_at'), 'installed policy observation', 1)
        clock = int(time.time()) if now is None else now
        if policy.get('complete') is not True or clock < observed:
            raise ValueError('Installed policy is incomplete or stale')
        receipt = {'engine': 'tantivy', 'build_id': manifest['active_build'], 'format': FORMAT, 'manifest_sha256': digest_json(manifest), 'bundle_sha256': manifest['sha256'], 'binary_sha256': manifest['binary_sha256'], 'installed_at': clock, 'passed': True, 'runtime_qualified': False, **gates}
        with (staging / 'installation.json').open('x') as output:
            output.write(json.dumps(receipt, sort_keys=True) + '\n'); output.flush(); os.fsync(output.fileno())
        destination.mkdir()
        (staging / 'index').rename(destination / 'index')
        (staging / 'installation.json').rename(destination / 'installation.json')
        for path in [destination / 'index', destination, destination.parent]:
            descriptor = os.open(path, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    return receipt


def qualify_route(status, manifest, installation, now=None, observed_origin=None, provider_attestation=None, **bounds):
    validate_manifest(manifest, manifest['active_build'])
    gates = validate_runtime(status, manifest, now, **bounds)
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
    receipt['test_only'] = receipt['test_only'] or provider['provider_attestation_test_only']
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

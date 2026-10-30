import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from contextlib import contextmanager

from build import digest_file
from convert import key, identifier, FIELDS


class Publisher:
    def __init__(self, config, bucket, rclone='rclone'):
        self.command = [rclone, '--config', str(config)]
        self.bucket = bucket

    def remote(self, object_key):
        return f'r2:{self.bucket}/{key(object_key)}'

    def verify(self, local, object_key):
        self.verify_expected(object_key, local.stat().st_size, digest_file(local))

    def verify_expected(self, object_key, expected_size, expected_sha256):
        print(f"verify {object_key}", flush=True)
        process = subprocess.Popen(
            [*self.command, 'cat', self.remote(object_key)], stdout=subprocess.PIPE,
        )
        digest = hashlib.sha256()
        size = 0
        try:
            while data := process.stdout.read(1024 * 1024):
                digest.update(data)
                size += len(data)
        finally:
            process.stdout.close()
            code = process.wait()
        if code or size != expected_size or digest.hexdigest() != expected_sha256:
            raise ValueError(f'remote verification failed: {object_key}')

    def immutable(self, local, object_key):
        result = subprocess.run(
            [*self.command, 'lsjson', '--stat', self.remote(object_key)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and not json.loads(result.stdout)['IsDir']:
            self.verify(local, object_key)
            return
        if result.returncode not in (0, 3, 4):
            raise RuntimeError(f'cannot inspect remote artifact: {object_key}: {result.stderr}')
        print(f'upload {object_key}', flush=True)
        subprocess.run(
            [*self.command, 'copyto', '--immutable', str(local), self.remote(object_key)],
            check=True,
        )
        self.verify(local, object_key)

    def publish(self, generation, legacy_database, legacy_manifest, pointer=None, *, immutable_only=False):
        if immutable_only:
            if pointer is not None:
                raise ValueError('immutable-only publication prohibits a pointer')
        else:
            key(pointer)
        if pointer == 'latest.json' and os.environ.get('INDEX_ALLOW_PROD') != '1':
            raise ValueError('production pointer requires INDEX_ALLOW_PROD=1')
        generation = Path(generation)
        legacy_database, legacy_manifest = Path(legacy_database), Path(legacy_manifest)
        manifest_path = generation / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        legacy = json.loads(legacy_manifest.read_bytes())
        if manifest['manifest_version'] != 2 or manifest['format_version'] != 5 or legacy['manifest_version'] != 1:
            raise ValueError('require compact generation and legacy twin')
        identifier(manifest['active_build'])
        identifier(legacy['active_build'])
        for field in (*FIELDS, 'normalize_version', 'scoring_version'):
            if manifest[field] != legacy[field]:
                raise ValueError('legacy twin serving metadata mismatch: ' + field)
        for candidate in (manifest, legacy):
            if not candidate['snapshot_url'].endswith('/' + candidate['active_build'] + '/index.db'):
                raise ValueError('snapshot key identity mismatch')
        if manifest['prefix_map']['object_key'] != manifest['snapshot_url'] + '.prefix':
            raise ValueError('map key identity mismatch')
        twin = manifest['legacy_twin']
        if twin['manifest']['object_key'] != legacy['snapshot_url'].rsplit('/', 1)[0] + '/manifest.json':
            raise ValueError('twin manifest key identity mismatch')
        if (twin['build_id'] != legacy['active_build']
            or twin['source_watermark'] != legacy['source_watermark']
            or manifest['source_watermark'] != legacy['source_watermark']
            or manifest['active_build'] == legacy['active_build']):
            raise ValueError('legacy twin identity/watermark mismatch')
        artifacts = [
            (legacy_database, dict(object_key=legacy['snapshot_url'], byte_size=legacy['byte_size'], sha256=legacy['sha256'])),
            (legacy_manifest, twin['manifest']),
            (generation / 'index.db', dict(object_key=manifest['snapshot_url'], byte_size=manifest['byte_size'], sha256=manifest['sha256'])),
            (generation / 'index.db.prefix', manifest['prefix_map']),
        ]
        keys = [item['object_key'] for _, item in artifacts]
        manifest_key = key(manifest['snapshot_url'].rsplit('/', 1)[0] + '/manifest.json')
        publication_keys = [*keys, manifest_key] + ([] if immutable_only else [pointer])
        if len(set(publication_keys)) != len(publication_keys):
            raise ValueError('artifact/pointer key collision')
        for local, artifact in artifacts:
            key(artifact['object_key'])
            if local.stat().st_size != artifact['byte_size'] or digest_file(local) != artifact['sha256']:
                raise ValueError(f'local verification failed: {local.name}')
        for local, artifact in artifacts:
            self.immutable(local, artifact['object_key'])
        self.immutable(manifest_path, manifest_key)
        if immutable_only:
            return manifest['active_build']
        print(f'publish pointer {pointer}', flush=True)
        subprocess.run([*self.command, 'copyto', str(manifest_path), self.remote(pointer)], check=True)
        self.verify(manifest_path, pointer)
        return manifest['active_build']

    def publish_tantivy(self, generation, source_manifest):
        generation, source_manifest, manifest_path, manifest, source = self.tantivy_artifacts(generation, source_manifest)
        bundle = generation / 'index.tar'
        if bundle.stat().st_size != manifest['byte_size'] or digest_file(bundle) != manifest['sha256']:
            raise ValueError('local verification failed: index.tar')
        self.verify(source_manifest, f"releases/search/{source['active_build']}/manifest.json")
        self.immutable(bundle, manifest['object_key'])
        self.immutable(manifest_path, f"releases/search-tantivy/{manifest['active_build']}/manifest.json")
        return manifest['active_build']

    def verify_tantivy(self, generation, source_manifest):
        _, source_manifest, manifest_path, manifest, source = self.tantivy_artifacts(generation, source_manifest)
        self.verify(source_manifest, f"releases/search/{source['active_build']}/manifest.json")
        self.verify_expected(manifest['object_key'], manifest['byte_size'], manifest['sha256'])
        self.verify(manifest_path, f"releases/search-tantivy/{manifest['active_build']}/manifest.json")
        return manifest['active_build']

    def tantivy_artifacts(self, generation, source_manifest):
        location = Path(__file__).resolve().parents[1] / 'snapshot_refresh/tantivy_install.py'
        spec = importlib.util.spec_from_file_location('tantivy_install', location)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        generation, source_manifest = Path(generation), Path(source_manifest)
        manifest_path = generation / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        source_raw = source_manifest.read_bytes()
        adapter.validate_source_manifest(manifest, source_raw)
        source = json.loads(source_raw)
        native_build, _ = adapter.native_anchor_binding(manifest)
        if source.get('active_build') != native_build:
            raise ValueError('Native rollback anchor identity mismatch')
        if any(field in manifest for field in ['image_digest', 'guest', 'volume_id', 'volume_size_gib', 'public_service_ports', 'provider_app']):
            raise ValueError('Publish a portable Tantivy artifact without deployment bindings')
        source_key = f"releases/search/{source['active_build']}/manifest.json"
        if source.get('snapshot_url') != source_key.removesuffix('manifest.json') + 'index.db':
            raise ValueError('Native source object identity mismatch')
        return generation, source_manifest, manifest_path, manifest, source


@contextmanager
def configuration(path):
    if path is not None:
        yield path
        return
    names = {
        'endpoint': 'INDEX_R2_ENDPOINT',
        'access_key_id': 'INDEX_R2_ACCESS_KEY_ID',
        'secret_access_key': 'INDEX_R2_SECRET_ACCESS_KEY',
    }
    values = {field: os.environ[name] for field, name in names.items()}
    if any(not value or any(ch in value for ch in '\r\n\0') for value in values.values()):
        raise ValueError('invalid R2 configuration')
    with tempfile.NamedTemporaryFile(mode='w', prefix='typeahead-rclone-', suffix='.conf') as config:
        config.write('[r2]\ntype = s3\nprovider = Cloudflare\nacl = private\nno_check_bucket = true\nregion = auto\n')
        for field, value in values.items():
            config.write(f'{field} = {value}\n')
        config.flush()
        yield Path(config.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--engine', choices=['native', 'tantivy'], default='native')
    parser.add_argument('--legacy-database', type=Path)
    parser.add_argument('--legacy-manifest', type=Path)
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--bucket', default=os.environ.get('INDEX_R2_BUCKET'))
    routing = parser.add_mutually_exclusive_group()
    routing.add_argument('--pointer')
    routing.add_argument('--immutable-only', action='store_true')
    parser.add_argument('--rclone', default=os.environ.get('INDEX_RCLONE_BIN', 'rclone'))
    args = parser.parse_args()
    if not args.bucket:
        parser.error('--bucket or INDEX_R2_BUCKET is required')
    if args.engine == 'native' and not all([args.legacy_database, args.legacy_manifest, args.pointer or args.immutable_only]):
        parser.error('native requires --legacy-database, --legacy-manifest and either --pointer or --immutable-only')
    if args.engine == 'native' and args.verify_only:
        parser.error('--verify-only applies to immutable Tantivy publication only')
    if args.engine == 'tantivy' and (not args.source_manifest or args.pointer or args.immutable_only or args.legacy_database or args.legacy_manifest):
        parser.error('tantivy requires --source-manifest and prohibits native/pointer arguments')
    with configuration(args.config) as config:
        publisher = Publisher(config, args.bucket, args.rclone)
        if args.engine == 'tantivy':
            operation = publisher.verify_tantivy if args.verify_only else publisher.publish_tantivy
            print(operation(args.generation, args.source_manifest))
        else:
            print(publisher.publish(args.generation, args.legacy_database, args.legacy_manifest, args.pointer, immutable_only=args.immutable_only))


if __name__ == '__main__':
    main()

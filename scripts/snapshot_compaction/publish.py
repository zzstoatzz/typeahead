import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from contextlib import contextmanager


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def key(value):
    if not value or len(value) > 1024 or any(
        part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', part)
        for part in value.split('/')
    ):
        raise ValueError('invalid object key')
    return value


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

    def publish_tantivy(self, generation):
        generation, manifest_path, manifest = self.tantivy_artifacts(generation)
        bundle = generation / 'index.tar'
        if bundle.stat().st_size != manifest['byte_size'] or digest_file(bundle) != manifest['sha256']:
            raise ValueError('local verification failed: index.tar')
        self.immutable(bundle, manifest['object_key'])
        self.immutable(manifest_path, f"releases/search-tantivy/{manifest['active_build']}/manifest.json")
        return manifest['active_build']

    def verify_tantivy(self, generation):
        _, manifest_path, manifest = self.tantivy_artifacts(generation)
        self.verify_expected(manifest['object_key'], manifest['byte_size'], manifest['sha256'])
        self.verify(manifest_path, f"releases/search-tantivy/{manifest['active_build']}/manifest.json")
        return manifest['active_build']

    def tantivy_artifacts(self, generation):
        location = Path(__file__).resolve().parents[1] / 'snapshot_refresh/tantivy_install.py'
        spec = importlib.util.spec_from_file_location('tantivy_install', location)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        generation = Path(generation)
        manifest_path = generation / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        adapter.validate_source_provenance(manifest)
        if any(field in manifest for field in ['image_digest', 'guest', 'volume_id', 'volume_size_gib', 'public_service_ports', 'provider_app']):
            raise ValueError('Publish a portable Tantivy artifact without deployment bindings')
        return generation, manifest_path, manifest


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
    parser.add_argument('--engine', choices=['tantivy'], default='tantivy')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--bucket', default=os.environ.get('INDEX_R2_BUCKET'))
    parser.add_argument('--rclone', default=os.environ.get('INDEX_RCLONE_BIN', 'rclone'))
    args = parser.parse_args()
    if not args.bucket:
        parser.error('--bucket or INDEX_R2_BUCKET is required')
    with configuration(args.config) as config:
        publisher = Publisher(config, args.bucket, args.rclone)
        operation = publisher.verify_tantivy if args.verify_only else publisher.publish_tantivy
        print(operation(args.generation))


if __name__ == '__main__':
    main()

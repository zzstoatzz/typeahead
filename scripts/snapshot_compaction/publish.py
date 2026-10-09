import argparse
import hashlib
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
        print(f"verify {object_key}", flush=True)
        expected = digest_file(local)
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
        if code or size != local.stat().st_size or digest.hexdigest() != expected:
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

    def publish(self, generation, legacy_database, legacy_manifest, pointer):
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
        if len(set([*keys, manifest_key, pointer])) != len(keys) + 2:
            raise ValueError('artifact/pointer key collision')
        for local, artifact in artifacts:
            key(artifact['object_key'])
            if local.stat().st_size != artifact['byte_size'] or digest_file(local) != artifact['sha256']:
                raise ValueError(f'local verification failed: {local.name}')
        for local, artifact in artifacts:
            self.immutable(local, artifact['object_key'])
        self.immutable(manifest_path, manifest_key)
        print(f'publish pointer {pointer}', flush=True)
        subprocess.run([*self.command, 'copyto', str(manifest_path), self.remote(pointer)], check=True)
        self.verify(manifest_path, pointer)
        return manifest['active_build']


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
    parser.add_argument('--legacy-database', type=Path, required=True)
    parser.add_argument('--legacy-manifest', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--bucket', default=os.environ.get('INDEX_R2_BUCKET'))
    parser.add_argument('--pointer', required=True)
    parser.add_argument('--rclone', default=os.environ.get('INDEX_RCLONE_BIN', 'rclone'))
    args = parser.parse_args()
    if not args.bucket:
        parser.error('--bucket or INDEX_R2_BUCKET is required')
    with configuration(args.config) as config:
        publisher = Publisher(config, args.bucket, args.rclone)
        print(publisher.publish(args.generation, args.legacy_database, args.legacy_manifest, args.pointer))


if __name__ == '__main__':
    main()

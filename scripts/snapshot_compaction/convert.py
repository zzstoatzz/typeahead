import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from build import build, digest_file, sync_directory
from quiet import run


FIELDS = (
    'source_watermark', 'actor_count', 'prefix_count', 'top_n_short',
    'top_n_medium', 'top_n_long', 'overlay_top_n', 'hydrate_cap',
    'cjk_key_count', 'cjk_actor_count',
)


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('invalid build identity')
    return value


def key(value):
    if not value or len(value) > 1024 or any(
        part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', part)
        for part in value.split('/')
    ):
        raise ValueError('invalid object key')
    return value


def convert(source, source_manifest, output, build_id, object_prefix):
    source = Path(source).resolve()
    source_manifest = Path(source_manifest).resolve()
    output = Path(output).absolute()
    identifier(build_id)
    key(object_prefix)
    if output.exists():
        raise FileExistsError(output)
    original_manifest = source_manifest.read_bytes()
    legacy = json.loads(original_manifest)
    identifier(legacy['active_build'])
    key(legacy['snapshot_url'])
    if (legacy['manifest_version'] != 1 or legacy.get('format_version', 0) != 0
        or legacy.get('prefix_map') is not None or legacy.get('legacy_twin') is not None
        or legacy['active_build'] == build_id):
        raise ValueError('require a legacy manifest and a distinct compact identity')
    if source.stat().st_size != legacy['byte_size'] or digest_file(source) != legacy['sha256']:
        raise ValueError('source checksum/size mismatch')
    for suffix in ('-wal', '-journal'):
        if Path(str(source) + suffix).exists():
            raise ValueError('source must be a closed immutable checkpoint')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as db:
        meta = dict(db.execute('SELECT key,value FROM index_meta'))
        if meta['build_id'] != legacy['active_build']:
            raise ValueError('source build mismatch')
        for field in ('normalize_version', 'scoring_version'):
            if meta[field] != legacy[field]:
                raise ValueError('source version mismatch: ' + field)
        for field in FIELDS:
            if int(meta[field]) != legacy[field]:
                raise ValueError('source metadata mismatch: ' + field)
        for field, table in (('actor_count', 'actor_compact_base'), ('prefix_count', 'prefix_top_base')):
            if db.execute('SELECT count(*) FROM ' + table).fetchone()[0] != legacy[field]:
                raise ValueError('source count mismatch: ' + field)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.compact-release-') as temp:
        staged = Path(temp) / 'generation'
        evidence = build(source, staged)
        database = staged / 'index.db'
        with sqlite3.connect(database) as db:
            db.execute("UPDATE index_meta SET value=? WHERE key='build_id'", (build_id,))
            db.commit()
        manifest = dict(legacy)
        manifest.update(
            manifest_version=2, format_version=5, active_build=build_id,
            previous_build=legacy['active_build'],
            snapshot_url=key(f'{object_prefix}/{build_id}/index.db'),
            byte_size=database.stat().st_size, sha256=digest_file(database),
            prefix_map=dict(
                object_key=key(f'{object_prefix}/{build_id}/index.db.prefix'),
                byte_size=evidence['prefix_bytes'],
                sha256=evidence['sha256']['index.db.prefix'],
            ),
            legacy_twin=dict(
                manifest=dict(
                    object_key=key(legacy['snapshot_url'].rsplit('/', 1)[0] + '/manifest.json'),
                    byte_size=len(original_manifest), sha256=digest_file(source_manifest),
                ),
                build_id=legacy['active_build'], source_watermark=legacy['source_watermark'],
            ),
        )
        (staged / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        if digest_file(source) != legacy['sha256'] or source_manifest.read_bytes() != original_manifest:
            raise ValueError('source changed during conversion')
        for path in staged.iterdir():
            with path.open('rb') as file:
                os.fsync(file.fileno())
        sync_directory(staged)
        if output.exists():
            raise FileExistsError(output)
        os.rename(staged, output)
        sync_directory(output.parent)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--object-prefix', default='staging/compaction')
    parser.add_argument('--unlimited-cpu', action='store_true', help='Skip Linux quiet supervision for controlled runs')
    args = parser.parse_args()
    if not args.unlimited_cpu:
        sys.exit(run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--unlimited-cpu']))
    print(json.dumps(convert(args.source, args.manifest, args.output, args.build_id, args.object_prefix), indent=2))


if __name__ == '__main__':
    main()

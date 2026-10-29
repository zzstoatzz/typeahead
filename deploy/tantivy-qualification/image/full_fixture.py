"""Small synthetic source for the staged cloud lifecycle pilot; never live data."""
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

OWNER = 'synthetic-staged-fixture-v1'
MAX_BYTES = 8 * 1024 * 1024


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(root, app='/app'):
    root = Path(root).absolute()
    app = Path(app)
    if root.exists() or root.parent.resolve() != root.parent:
        raise ValueError('synthetic fixture requires an absent canonical root')
    root.mkdir(mode=0o700)
    (root/'owner').write_text(OWNER)
    source = root/'source.db'
    floor = int(os.environ.get('QUAL_WATERMARK',str(int(time.time())-60)))
    run = os.environ.get('QUAL_RUN_ID','offlinefixture')
    if not 10**9<=floor<10**10 or not re.fullmatch('[a-z0-9]{8,16}',run):
        raise ValueError('synthetic fixture shared identity')
    deterministic_build = f'b{floor}-pilot-{run}'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
        db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)', [
            (f'did:fixture:{i:03}', f'actor{i}.fixture.test', f'Actor {i}', '', '[]',
             'https://local.test' if i % 2 else 'https://remote.test', '', '{}', 0, i, floor)
            for i in range(128)
        ])
        db.commit()
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'HOME', 'LANG'}}
    env.update(MODE='indexer', INDEX_CHANNEL='local', INDEX_SOURCE='export',
               INDEX_SAMPLE_LIMIT='0', INDEX_PDS_MAX_ACTORS='10000',
               INDEX_REUSE_SOURCE=str(source), INDEX_BUILD_ROOT=str(root/'build'),
               INDEXER_VERSION='synthetic-staged-pilot')
    subprocess.run([str(app/'bin/typeahead-ingester')], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=60)
    sys.path.insert(0, str(app/'input'))
    from reference_build import build as compact
    legacy = list((root/'build').glob('build-*/index.db'))
    if len(legacy) != 1:
        raise ValueError('synthetic fixture generation identity')
    with closing(sqlite3.connect(legacy[0])) as db:
        db.executemany('INSERT OR REPLACE INTO index_meta(key,value) VALUES(?,?)', [('build_id',deterministic_build),('built_at',str(floor)),('source_watermark',str(floor))])
        db.commit();db.execute('VACUUM')
    compact(legacy[0], root/'release')
    database = root/'release/index.db'
    prefix = root/'release/index.db.prefix'
    with closing(sqlite3.connect(database)) as db:
        metadata = dict(db.execute('SELECT key,value FROM index_meta'))
    if int(metadata['actor_count']) != 128 or database.stat().st_size + prefix.stat().st_size > MAX_BYTES:
        raise ValueError('synthetic fixture size or count')
    identity = metadata['build_id']
    manifest = {
        'manifest_version': 2, 'format_version': 5, 'active_build': identity,
        'source_watermark': floor, 'actor_count': 128,
        'prefix_count': int(metadata['prefix_count']),
        'normalize_version': metadata['normalize_version'], 'scoring_version': metadata['scoring_version'],
        'byte_size': database.stat().st_size, 'sha256': sha(database),
        'snapshot_url': f'releases/search/{identity}/index.db',
        'prefix_map': {'object_key': f'releases/search/{identity}/index.db.prefix',
                       'byte_size': prefix.stat().st_size, 'sha256': sha(prefix)},
    }
    (root/'manifest.json').write_text(json.dumps(manifest))
    return {'manifest': manifest, 'source': root/'release'}


def cleanup(root):
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root or (root/'owner').read_text() != OWNER:
        raise ValueError('synthetic fixture cleanup ownership')
    shutil.rmtree(root)

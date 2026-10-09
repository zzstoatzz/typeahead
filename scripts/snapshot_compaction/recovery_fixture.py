import argparse
import json
from pathlib import Path
import sqlite3
import struct

from build import digest_file
from convert import convert


def create(root):
    legacy = root / 'builds/legacy'
    legacy.mkdir(parents=True)
    database = legacy / 'index.db'
    did = b'did:plc:recovery'
    fields = [b'recovery.test', b'Recovery', b'', b'[]', b'', b'', b'{}']
    actor = struct.pack('<BBf', 1, 0, 1) + b''.join(struct.pack('<H', len(v)) + v for v in fields)
    posting = struct.pack('<BHfH', 1, 1, 1, len(did)) + did
    manifest = dict(
        manifest_version=1, active_build='legacy', previous_build=None,
        promoted_at=100, snapshot_url='builds/legacy/index.db', source_watermark=100,
        actor_count=1, prefix_count=5, normalize_version='v2-cjk-bigram',
        scoring_version='v1-quality-score', top_n_short=300, top_n_medium=200,
        top_n_long=100, overlay_top_n=100, hydrate_cap=50, builder_version='test',
        cjk_key_count=50000, cjk_actor_count=1,
    )
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY, blob BLOB NOT NULL) WITHOUT ROWID;
            CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY, dids BLOB NOT NULL) WITHOUT ROWID;
            CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        db.execute('INSERT INTO actor_compact_base VALUES(?,?)', (did.decode(), actor))
        db.executemany('INSERT INTO prefix_top_base VALUES(?,?)', [('h:' + q, posting) for q in 'asmjt'])
        metadata = dict(manifest, build_id='legacy')
        db.executemany('INSERT INTO index_meta VALUES(?,?)', [(k, str(v)) for k, v in metadata.items()])
    manifest.update(byte_size=database.stat().st_size, sha256=digest_file(database))
    (legacy / 'manifest.json').write_text(json.dumps(manifest) + '\n')
    convert(database, legacy / 'manifest.json', root / 'builds/compact', 'compact', 'builds')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    create(parser.parse_args().root)

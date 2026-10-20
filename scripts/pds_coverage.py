#!/usr/bin/env python3
"""Offline PDS repair queue and compare-and-set statements; no live writer."""
from contextlib import closing
import argparse
import json
import itertools
import struct
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

APPLY_SQL = """UPDATE actors SET pds=:pds, updated_at=:written_at
WHERE did=:did AND pds='' AND updated_at=:observed_updated_at
RETURNING did,pds,updated_at"""
ROLLBACK_SQL = """UPDATE actors SET pds='', updated_at=:rollback_at
WHERE did=:did AND pds=:pds AND updated_at=:written_at
RETURNING did,pds,updated_at"""


def pds_from_document(did, document):
    if document.get('id') != did:
        raise ValueError('DID document subject mismatch')
    endpoints = []
    for service in document.get('service', []):
        if service.get('id') not in ('#atproto_pds', did + '#atproto_pds'):
            continue
        if service.get('type') != 'AtprotoPersonalDataServer':
            raise ValueError('Unexpected PDS service type')
        endpoint = service.get('serviceEndpoint')
        if not isinstance(endpoint, str):
            raise ValueError('Invalid PDS endpoint')
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Invalid HTTP(S) PDS endpoint')
        _ = parsed.port
        endpoints.append(endpoint.rstrip('/'))
    if len(endpoints) != 1:
        raise ValueError('Expected exactly one PDS service')
    return endpoints[0]


def unpack_pds(blob):
    if len(blob)<6 or blob[0]!=1:raise ValueError('Unsupported actor blob')
    offset=6;fields=[]
    for _ in range(7):
        if offset+2>len(blob):raise ValueError('Truncated actor blob')
        length=struct.unpack_from('<H',blob,offset)[0];offset+=2
        if offset+length>len(blob):raise ValueError('Truncated actor field')
        fields.append(blob[offset:offset+length].decode());offset+=length
    if offset!=len(blob):raise ValueError('Unexpected actor trailer')
    return fields[4]



def prepare(source, destination, batch_size=10000, limit=None, source_format="export"):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Queue already exists; refusing replacement')
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError('Batch size and limit must be positive')
    if source_format not in ('export','snapshot'):raise ValueError('Unknown source format')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        src.execute('BEGIN')
        destination.open('xb').close()
        dst = sqlite3.connect(destination)
        try:
            dst.executescript('''
              CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE candidates(
                did TEXT PRIMARY KEY, observed_updated_at INTEGER,
                state TEXT NOT NULL DEFAULT 'pending', pds TEXT,
                resolved_at INTEGER, document TEXT, written_at INTEGER,
                CHECK(state IN ('pending','resolved','applied','conflict','unresolved'))
              ) WITHOUT ROWID;
            ''')
            dst.executemany('INSERT INTO metadata VALUES (?,?)', [
                ('source', str(source)), ('created_at', str(int(time.time()))),
                ('complete', 'false'), ('source_size', str(source.stat().st_size)),
                ('source_format',source_format),('version_provenance','source_export' if source_format=='export' else 'live_read_required')])
            dst.commit()
            if source_format=='export':
                cursor=iter(src.execute("SELECT did,updated_at FROM actors WHERE pds='' ORDER BY rowid"))
            else:
                meta=dict(src.execute('SELECT key,value FROM index_meta'))
                if not meta.get('build_id') or not meta.get('source_watermark'):
                    raise ValueError('Snapshot lacks provenance metadata')
                dst.executemany('INSERT INTO metadata VALUES (?,?)',[(k,meta[k]) for k in ('build_id','source_watermark')])
                cursor=((did,None) for did,blob in src.execute('SELECT did,blob FROM actor_compact_base ORDER BY did') if unpack_pds(blob)=='')
            count = 0
            while limit is None or count < limit:
                rows = list(itertools.islice(cursor,min(batch_size, limit-count) if limit else batch_size))
                if not rows:
                    break
                dst.executemany('INSERT INTO candidates(did,observed_updated_at) VALUES (?,?)', rows)
                dst.commit()
                count += len(rows)
            dst.execute("UPDATE metadata SET value='true' WHERE key='complete'")
            dst.execute('INSERT INTO metadata VALUES (?,?)', ('selected', str(count)))
            dst.execute('INSERT INTO metadata VALUES (?,?)', ('selection_limit', str(limit)))
            dst.commit()
            return count
        finally:
            dst.close()


def record_resolution(db, did, document, resolved_at):
    endpoint = pds_from_document(did, document)
    with db:
        result = db.execute("""UPDATE candidates SET pds=?,resolved_at=?,document=?,state='resolved'
            WHERE did=? AND state IN ('pending','unresolved','resolved')""",
            (endpoint, resolved_at, json.dumps(document, sort_keys=True), did))
        if result.rowcount != 1:
            raise ValueError('Candidate absent or already applied/conflicted')
    return endpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--source-format',choices=('export','snapshot'),default='export')
    args = parser.parse_args()
    print(json.dumps({'selected': prepare(args.source, args.destination, limit=args.limit,source_format=args.source_format)}))


if __name__ == '__main__':
    main()

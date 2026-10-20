#!/usr/bin/env python3
"""Reconstruct candidate PLC identities offline. Output is NOT approved for live apply."""
from contextlib import closing
import argparse
import datetime
import gzip
import hashlib
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

from pds_coverage import pds_from_document

spec = importlib.util.spec_from_file_location('plc_reader', Path(__file__).with_name('plc-identity-sync.py'))
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS plc_latest(
  did TEXT PRIMARY KEY, created_at TEXT NOT NULL, cid TEXT NOT NULL,
  pds TEXT, status TEXT NOT NULL, bundle TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS plc_bundles(
  name TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
  sha256 TEXT NOT NULL,operations INTEGER NOT NULL,matched INTEGER NOT NULL
) WITHOUT ROWID;
'''
UPSERT = '''INSERT INTO plc_latest VALUES(?,?,?,?,?,?) ON CONFLICT(did) DO UPDATE SET
created_at=excluded.created_at,cid=excluded.cid,pds=excluded.pds,
status=excluded.status,bundle=excluded.bundle WHERE excluded.created_at>plc_latest.created_at'''


def parse_operation(record, bundle):
    did, created, cid = record['did'], record['createdAt'], record['cid']
    if not isinstance(did,str) or not did.startswith('did:plc:') or not cid:
        raise ValueError('Invalid PLC identity metadata')
    parsed=datetime.datetime.fromisoformat(created.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.microsecond % 1000:
        raise ValueError('PLC timestamp needs timezone and millisecond precision')
    created=parsed.astimezone(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    if 'nullified' not in record or not isinstance(record['nullified'],bool):
        raise ValueError('Expected legacy export with explicit nullified state')
    if record['nullified']:
        return None
    op = record['operation']
    endpoint, status = None, 'unresolved'
    if op.get('type') == 'plc_tombstone':
        status = 'tombstoned'
    else:
        service = ({'type':'AtprotoPersonalDataServer','endpoint':op.get('service')}
                   if op.get('type') == 'create' else (op.get('services') or {}).get('atproto_pds', {}))
        if op.get('type') in ('create','plc_operation'):
            doc={'id':did,'service':[{'id':'#atproto_pds','type':service.get('type'),'serviceEndpoint':service.get('endpoint')}]}
            try:
                endpoint = pds_from_document(did,doc)
                status = 'resolved'
            except (ValueError,TypeError):
                pass
    return did,created,cid,endpoint,status,bundle


def store_rows(db, rows):
    for row in rows:
        old = db.execute('SELECT created_at,cid FROM plc_latest WHERE did=?',(row[0],)).fetchone()
        if old and old[0] == row[1] and old[1] != row[2]:
            raise ValueError('Conflicting operations at identical DID timestamp')
        db.execute(UPSERT,row)


class DigestReader:
    def __init__(self, raw):
        self.raw=raw
        self.hash=hashlib.sha256()
    def read(self,n=-1):
        value=self.raw.read(n)
        self.hash.update(value)
        return value


def consume_bundle(db, candidates, path, expected_sha256=None):
    path=Path(path)
    stat=path.stat()
    started=time.monotonic();seen=matched=0;pending=[]
    with path.open('rb') as raw:
        digest=DigestReader(raw)
        with gzip.GzipFile(fileobj=digest) as compressed:
            import io
            with io.TextIOWrapper(compressed) as text:
                for record in reader._iter_json_values(text):
                    seen+=1
                    if record.get('did') not in candidates:
                        continue
                    row=parse_operation(record,path.name)
                    if row is not None:
                        pending.append(row);matched+=1
                    if len(pending)>=1000:
                        with db: store_rows(db,pending)
                        pending=[]
        with db: store_rows(db,pending)
    after=path.stat()
    if (after.st_size,after.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):
        raise ValueError('Bundle changed while reading')
    if expected_sha256 is not None and digest.hash.hexdigest()!=expected_sha256:
        raise ValueError('Consumed bundle checksum differs from manifest')
    with db:
        db.execute('INSERT INTO plc_bundles VALUES(?,?,?,?,?,?)',(path.name,stat.st_size,stat.st_mtime_ns,digest.hash.hexdigest(),seen,matched))
    print(json.dumps({'event':'bundle_complete','bundle':path.name,'operations':seen,'matched':matched,'seconds':round(time.monotonic()-started,2)}),flush=True)


def reconstruct(queue, directory):
    directory=Path(directory)
    files=sorted(directory.glob('*.jsonl.gz'))
    if not files:
        raise ValueError('No bundles')
    weeks=[int(p.name.split('.')[0]) for p in files]
    if weeks[0] != 1668643200 or any(b-a != 604800 for a,b in zip(weeks,weeks[1:])):
        raise ValueError('Bundle coverage has missing weeks')
    with closing(sqlite3.connect(queue)) as db:
        if db.execute("SELECT value FROM metadata WHERE key='complete'").fetchone() != ('true',):
            raise ValueError('Candidate preparation incomplete')
        db.executescript(SCHEMA)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_reconstruction_complete','false')")
        db.commit()
        candidates={r[0] for r in db.execute("SELECT did FROM candidates WHERE did LIKE 'did:plc:%'")}
        print(json.dumps({'event':'loaded_candidates','count':len(candidates)}),flush=True)
        for path in files:
            stat=path.stat()
            previous=db.execute('SELECT size,mtime_ns FROM plc_bundles WHERE name=?',(path.name,)).fetchone()
            if previous:
                if previous != (stat.st_size,stat.st_mtime_ns):
                    raise ValueError('Previously consumed bundle changed')
                continue
            consume_bundle(db,candidates,path)
        with db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_reconstruction_complete','true')")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_bundle_end',?)",(str(weeks[-1]+604800),))
        print(json.dumps({'event':'reconstruction_complete','states':db.execute('SELECT status,count(*) FROM plc_latest GROUP BY status').fetchall(),'apply_ready':False}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path);parser.add_argument('bundles',type=Path)
    args=parser.parse_args();reconstruct(args.queue,args.bundles)

"""Add bounded historical nonsearchable candidates to a verified snapshot queue."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def augment(queue, sidecar):
    raw=Path(sidecar).read_bytes()
    if len(raw)>1024*1024:
        raise ValueError('Sidecar exceeds metadata budget')
    data=json.loads(raw)
    if data['requires_current_live_read'] is not True or not 0<=len(data['rows'])<=10000:
        raise ValueError('Unqualified sidecar')
    dids=set()
    for did,version in data['rows']:
        if not isinstance(did,str) or not did.startswith('did:') or did in dids or type(version) is not int or version<0:
            raise ValueError('Invalid or duplicate candidate')
        dids.add(did)
    checksum=hashlib.sha256(raw).hexdigest()
    with closing(sqlite3.connect(Path(queue).resolve().as_uri()+'?mode=rw',uri=True)) as db,db:
        meta=dict(db.execute('SELECT key,value FROM metadata'))
        if (meta.get('complete')!='true' or meta.get('source_format')!='snapshot'
                or meta.get('version_provenance')!='live_read_required'
                or meta.get('build_id')!=data['build_id']
                or int(meta.get('source_watermark','-1'))!=data['source_watermark']):
            raise ValueError('Sidecar snapshot provenance mismatch')
        if 'nonsearchable_sidecar_sha256' in meta:
            if meta['nonsearchable_sidecar_sha256']!=checksum:
                raise ValueError('Sidecar changed after application')
            return {'added':0,'already_applied':True,'sha256':checksum}
        if 'plc_reconstruction_complete' in meta:
            raise ValueError('Augment before starting reconstruction')
        db.executemany('INSERT INTO candidates(did,observed_updated_at) VALUES(?,NULL)',((did,) for did in sorted(dids)))
        db.executemany('INSERT INTO metadata VALUES(?,?)',[
            ('nonsearchable_sidecar_sha256',checksum),('nonsearchable_sidecar_count',str(len(dids)))])
        count=db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        db.execute("UPDATE metadata SET value=? WHERE key='selected'",(str(count),))
    return {'added':len(dids),'already_applied':False,'sha256':checksum,'total':count}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path);parser.add_argument('sidecar',type=Path)
    args=parser.parse_args();print(json.dumps(augment(args.queue,args.sidecar)))

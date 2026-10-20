"""Consume verified PLC bundles one at a time with persistent transfer limits."""
import argparse
import fcntl
import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from pds_bundle_download import Budget, download
from pds_plc_reconstruct import SCHEMA, consume_bundle


def validate_manifest(manifest):
    if not manifest:
        raise ValueError('Empty manifest')
    for index, item in enumerate(manifest):
        if item['name'] != f'{1668643200+604800*index}.jsonl.gz':
            raise ValueError('Noncontiguous or unsafe bundle name')
        if type(item['size']) is not int or item['size'] < 1:
            raise ValueError('Invalid bundle size')
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            raise ValueError('Invalid bundle digest')


def run(queue, directory, manifest, base_url, byte_limit, reserve_bytes, rate):
    validate_manifest(manifest)
    if reserve_bytes < 1:
        raise ValueError('Disk reserve required')
    directory=Path(directory);directory.mkdir(exist_ok=True)
    with (directory/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        budget=Budget(directory/'transfer.db',byte_limit)
        with closing(sqlite3.connect(Path(queue).resolve().as_uri()+'?mode=rw',uri=True)) as db:
            if db.execute("SELECT value FROM metadata WHERE key='complete'").fetchone()!=('true',):
                raise ValueError('Candidate preparation incomplete')
            db.executescript(SCHEMA)
            encoded=json.dumps(manifest,sort_keys=True,separators=(',',':'))
            old=db.execute("SELECT value FROM metadata WHERE key='cloud_bundle_manifest'").fetchone()
            if old is not None and old!=(encoded,):
                raise ValueError('Manifest changed; explicit reconciliation required')
            with db:
                db.execute("INSERT OR IGNORE INTO metadata VALUES('cloud_bundle_manifest',?)",(encoded,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_reconstruction_complete','false')")
            candidates={row[0] for row in db.execute("SELECT did FROM candidates WHERE did LIKE 'did:plc:%'")}
            for item in manifest:
                path=directory/item['name']
                previous=db.execute('SELECT size,sha256 FROM plc_bundles WHERE name=?',(item['name'],)).fetchone()
                if previous is not None:
                    if previous!=(item['size'],item['sha256']):
                        raise ValueError('Consumed bundle differs from manifest')
                    if path.exists():
                        from pds_bundle_download import digest
                        if path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:
                            raise ValueError('Retained completed bundle changed')
                        path.unlink()
                    continue
                if shutil.disk_usage(directory).free < reserve_bytes+item['size']:
                    raise RuntimeError('Disk reserve would be exhausted')
                download(base_url.rstrip('/')+'/'+item['name'],path,item['size'],item['sha256'],budget,rate)
                consume_bundle(db,candidates,path,item['sha256'])
                path.unlink()
                if shutil.disk_usage(directory).free < reserve_bytes:
                    raise RuntimeError('Disk reserve exhausted while processing')
            with db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_reconstruction_complete','true')")
                db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_bundle_end',?)",(str(1668643200+604800*len(manifest)),))
            return {'bundles':len(manifest),'reserved_body_bytes':budget.used(),'apply_ready':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path)
    parser.add_argument('directory',type=Path)
    parser.add_argument('manifest',type=Path)
    parser.add_argument('--cloud-machine-id',required=True)
    parser.add_argument('--byte-limit',type=int,required=True)
    parser.add_argument('--reserve-bytes',type=int,required=True)
    parser.add_argument('--bytes-per-second',type=int,default=5*1024*1024)
    args=parser.parse_args()
    if Path('/etc/machine-id').read_text().strip()!=args.cloud_machine_id:
        raise SystemExit('Cloud machine identity mismatch; refusing network work')
    print(json.dumps(run(args.queue,args.directory,json.loads(args.manifest.read_text()),
        'https://plc.t3.storage.dev/plc.directory',args.byte_limit,args.reserve_bytes,args.bytes_per_second)))

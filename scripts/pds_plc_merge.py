#!/usr/bin/env python3
"""Reconcile a completed PLC tail with completed offline reconstruction."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from pds_plc_reconstruct import parse_operation, store_rows
from pds_plc_tail import page_result, timestamp


def validated_pages(directory):
    directory=Path(directory)
    config=json.loads((directory/'config.json').read_text())
    complete=json.loads((directory/'complete.json').read_text())
    if any(config[k]!=complete[k] for k in ('after','cutoff')) or not 1<=complete['pages']<=10000:
        raise ValueError('Invalid tail completion metadata')
    cursor=config['after']
    for index in range(complete['pages']):
        name=f'{index:06}.json'
        saved=json.loads((directory/name).read_text())
        if saved['after']!=cursor:raise ValueError('Tail cursor chain broken')
        encoded=json.dumps(saved['response'],sort_keys=True).encode()
        if hashlib.sha256(encoded).hexdigest()!=saved['sha256']:raise ValueError('Tail checksum mismatch')
        result=page_result(saved['response'],cursor,config['cutoff'])
        if result['complete'] != (index+1==complete['pages']):raise ValueError('Tail completion boundary mismatch')
        yield name,saved['sha256'],result['records']
        cursor=result['next_after']


def merge(queue,directory):
    directory=Path(directory)
    config=json.loads((directory/'config.json').read_text())
    with closing(sqlite3.connect(queue)) as db:
        meta=dict(db.execute('SELECT key,value FROM metadata'))
        if meta.get('plc_reconstruction_complete')!='true':raise ValueError('Historical reconstruction incomplete')
        if timestamp(config['after']).timestamp()>int(meta['plc_bundle_end']):raise ValueError('Gap between historical bundles and tail')
        db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_tail_complete','false')");db.commit()
        # Validate the entire input chain before updating even the offline identity state.
        for _ in validated_pages(directory):pass
        changed=0
        for name,digest,records in validated_pages(directory):
            with db:
                for record in records:
                    did=record['did']
                    if not db.execute('SELECT 1 FROM candidates WHERE did=?',(did,)).fetchone():continue
                    if record['nullified']:
                        db.execute("UPDATE plc_latest SET pds=NULL,status='unresolved' WHERE did=? AND cid=?",(did,record['cid']))
                    else:
                        store_rows(db,[parse_operation(record,str(directory/name)+'#'+digest)])
                    changed+=1
        with db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_tail_complete','true')")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_tail_cutoff',?)",(config['cutoff'],))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('plc_tail_source',?)",(str(directory.resolve()),))
        return {'matched_operations':changed,'cutoff':config['cutoff'],'apply_ready':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path);parser.add_argument('tail',type=Path)
    args=parser.parse_args();print(json.dumps(merge(args.queue,args.tail)))

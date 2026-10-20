#!/usr/bin/env python3
"""Bounded read-only verification of canary receipts in the native search overlay."""
import argparse
import json
import re
import shlex
import sqlite3
import subprocess
from contextlib import closing
from pds_coverage import unpack_pds




def verify(ledger,machine):
    with closing(sqlite3.connect(ledger)) as db:
        confirmed=db.execute("SELECT did,pds,receipt FROM attempts WHERE state='confirmed'").fetchall()
    if not 1<=len(confirmed)<=5:raise ValueError('Expected one to five confirmed canary receipts')
    if any(not re.fullmatch(r'did:plc:[a-z2-7]{24}',row[0]) for row in confirmed):raise ValueError('Invalid DID')
    dids=','.join("'"+row[0]+"'" for row in confirmed)
    sql=f'SELECT did,hex(blob) AS blob,updated_at FROM actor_overlay WHERE did IN ({dids})'
    command=shlex.join(['sqlite3','-readonly','-json','/data/pds-overlay/overlay.db',sql])
    result=subprocess.run(['fly','ssh','console','-a','typeahead-search-compact','--machine',machine,'-C',command],capture_output=True,text=True,check=True,timeout=30)
    rows={row['did']:row for row in json.loads(result.stdout or '[]')}
    checks=[]
    for did,pds,receipt in confirmed:
        row=rows.get(did)
        actual=unpack_pds(bytes.fromhex(row['blob'])) if row and row['blob'] else None
        written_at=json.loads(receipt)[2]
        checks.append({'did':did,'pds_matches':actual==pds,'version_caught_up':bool(row and row['updated_at']>=written_at)})
    return {'machine':machine,'checks':checks,'verified':all(r['pds_matches'] and r['version_caught_up'] for r in checks)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger');parser.add_argument('--machine',required=True)
    args=parser.parse_args();print(json.dumps(verify(args.ledger,args.machine)))

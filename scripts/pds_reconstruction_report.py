#!/usr/bin/env python3
"""Account for every offline candidate after PLC history and tail reconciliation."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def report(queue):
    with closing(sqlite3.connect(Path(queue).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        meta=dict(db.execute('SELECT key,value FROM metadata'))
        for key in ('complete','plc_reconstruction_complete','plc_tail_complete'):
            if meta.get(key)!='true':raise ValueError('Incomplete '+key)
        counts={}
        for status,count in db.execute("""
            SELECT CASE
              WHEN c.did NOT LIKE 'did:plc:%' THEN 'non_plc'
              WHEN p.did IS NULL THEN 'absent_from_plc'
              ELSE p.status END, count(*)
            FROM candidates c LEFT JOIN plc_latest p ON p.did=c.did
            GROUP BY 1
        """):
            counts[status]=count
        total=sum(counts.values())
        if total!=int(meta['selected']):raise ValueError('Candidate accounting mismatch')
        invalid=db.execute("SELECT count(*) FROM plc_latest WHERE status='resolved' AND (pds IS NULL OR pds='')").fetchone()[0]
        if invalid:raise ValueError('Resolved identities lack endpoints')
        endpoints=[{'pds':pds,'candidates':count} for pds,count in db.execute("""
            SELECT pds,count(*) FROM plc_latest WHERE status='resolved'
            GROUP BY pds ORDER BY count(*) DESC,pds
        """)]
        return {'source':meta['source'],'tail_cutoff':meta['plc_tail_cutoff'],
                'candidates':total,'states':counts,'endpoints':endpoints,
                'apply_ready':False,
                'limitations':['Historical identity reconstruction, not live source coverage.',
                               'Current identity reconciliation and guarded source reads are still required.',
                               'Endpoint counts are missing-PDS candidates, not complete host populations.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path)
    args=parser.parse_args()
    print(json.dumps(report(args.queue),indent=2))

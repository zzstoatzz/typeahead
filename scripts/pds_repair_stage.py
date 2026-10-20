"""Bounded repair stage orchestration; no production transport or enabled CLI.

The admission provider must validate candidate authority freshness, cloud host,
writer exclusivity, live health, serving disk reserve and rollback readiness.
A returned evidence dictionary is recorded, not independently certified here.
"""
import fcntl
import hashlib
import json
import math
import re
import time
from pathlib import Path

from pds_coverage import pds_from_document
from pds_repair_ledger import Ledger

READ_SQL = "SELECT did,pds,updated_at FROM actors WHERE did IN (SELECT value FROM json_each(:dids))"


def run(candidates, output, ledger_path, execute, admit, *, batch_size,
        max_rows, interval_seconds, max_write_seconds):
    if (type(batch_size) is not int or not 1<=batch_size<=100
            or type(max_rows) is not int or max_rows<1
            or not 1<=len(candidates)<=max_rows):
        raise ValueError('Explicit bounded stage and batch sizes required')
    if any(not math.isfinite(v) or v<=0 for v in (interval_seconds,max_write_seconds)):
        raise ValueError('Positive finite pacing and write limits required')
    dids=set()
    for row in candidates:
        did=row['did'];pds=row['pds']
        if not re.fullmatch(r'did:plc:[a-z2-7]{24}',did) or did in dids:
            raise ValueError('Invalid or duplicate candidate')
        dids.add(did)
        document={'id':did,'service':[{'id':'#atproto_pds','type':'AtprotoPersonalDataServer','serviceEndpoint':pds}]}
        if pds_from_document(did,document)!=pds or not row.get('provenance'):
            raise ValueError('Canonical endpoint and reconciliation provenance required')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    with Path(str(ledger_path)+'.stage-lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ledger=Ledger(ledger_path)
        encoded=json.dumps(candidates,sort_keys=True).encode()
        (output/'candidates.json').write_bytes(encoded)
        (output/'config.json').write_text(json.dumps(dict(batch_size=batch_size,max_rows=max_rows,
            interval_seconds=interval_seconds,max_write_seconds=max_write_seconds,
            candidates_sha256=hashlib.sha256(encoded).hexdigest())))
        with (output/'events.jsonl').open('x') as log:
            def event(kind,**fields):
                log.write(json.dumps(dict(at=time.time(),event=kind,**fields))+'\n');log.flush()
            def gate(phase):
                if (output/'STOP').exists():raise RuntimeError('Stage stop file present')
                proof=admit()
                if (output/'STOP').exists():raise RuntimeError('Stage stop file present')
                if not isinstance(proof,dict) or not proof:raise ValueError('Admission evidence required')
                event('admission',phase=phase,proof=proof)
            previous_write=None;confirmed=conflicts=skipped=0
            try:
                for offset in range(0,len(candidates),batch_size):
                    if previous_write is not None:
                        remaining=interval_seconds-(time.monotonic()-previous_write)
                        if remaining>0:time.sleep(remaining)
                    batch=candidates[offset:offset+batch_size]
                    gate('before_read')
                    rows=execute(READ_SQL,{'dids':json.dumps([r['did'] for r in batch])})
                    live={}
                    for row in rows:
                        if (len(row)!=3 or row[0] not in {r['did'] for r in batch}
                                or row[0] in live or type(row[2]) is not int):
                            raise ValueError('Invalid current source response')
                        live[row[0]]=row
                    attempts=[]
                    for item in batch:
                        row=live.get(item['did'])
                        if row is None or row[1]!='':
                            skipped+=1;event('skip',did=item['did'],reason='deleted_or_nonempty');continue
                        attempts.append(ledger.prepare(item['did'],row[2],item['pds']))
                    if not attempts:continue
                    gate('before_write')
                    previous_write=time.monotonic()
                    result=ledger.send_batch(attempts,execute)
                    elapsed=time.monotonic()-previous_write
                    confirmed+=result['confirmed'];conflicts+=result['conflict']
                    event('write',attempts=attempts,seconds=elapsed,result=result)
                    gate('after_write')
                    if elapsed>max_write_seconds:raise RuntimeError('Stage write exceeded latency budget')
                result=dict(confirmed=confirmed,conflicts=conflicts,skipped=skipped)
                event('complete',**result)
                return result
            except BaseException as error:
                event('stopped',error_type=type(error).__name__,detail=str(error))
                raise

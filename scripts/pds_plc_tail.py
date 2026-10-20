#!/usr/bin/env python3
"""Capture a bounded legacy PLC export tail for offline reconciliation."""
import argparse
import datetime
import hashlib
import json
import shutil
import urllib.error
from pds_bundle_download import Budget
import time
import urllib.parse
import urllib.request
from pathlib import Path


def timestamp(value):
    return datetime.datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=datetime.timezone.utc)


def page_result(records, after, cutoff):
    if not records:
        # An empty tail response does not prove the requested time boundary exists.
        raise ValueError('Empty PLC page before verified cutoff')
    dates=[timestamp(r['createdAt']) for r in records]
    if dates != sorted(dates) or dates[0] <= timestamp(after):
        raise ValueError('PLC timestamp order violated')
    for row in records:
        if not isinstance(row.get('nullified'),bool) or not row.get('cid') or not row.get('did'):
            raise ValueError('Incomplete PLC export record')
    done=dates[-1] >= timestamp(cutoff)
    # Replay the boundary timestamp: timestamp pagination otherwise loses ties.
    next_after=(dates[-1]-datetime.timedelta(milliseconds=1)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    if not done and timestamp(next_after) <= timestamp(after):
        raise ValueError('PLC timestamp tie cannot be paginated safely')
    return {'records':[r for r in records if timestamp(r['createdAt']) < timestamp(cutoff)],'next_after':next_after,'complete':done}


def fetch_page(url, budget, bytes_per_second, max_bytes=32*1024*1024):
    if bytes_per_second < 1 or max_bytes < 1:
        raise ValueError('Positive transfer bounds required')
    chunk=min(64*1024,max_bytes+1)
    budget.reserve(chunk)
    started=time.monotonic();payload=bytearray()
    request=urllib.request.Request(url,headers={'User-Agent':'typeahead-pds-coverage-repair','Accept-Encoding':'identity'})
    try:
        response=urllib.request.urlopen(request,timeout=30)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    with response:
        if response.status!=200:raise ValueError('Unexpected PLC status')
        while True:
            block=response.read(chunk)
            payload.extend(block)
            if len(payload)>max_bytes:raise ValueError('PLC response exceeds bound')
            delay=len(payload)/bytes_per_second-(time.monotonic()-started)
            if delay>0:time.sleep(delay)
            if len(block)<chunk:return bytes(payload)
            chunk=min(64*1024,max_bytes+1-len(payload))
            budget.reserve(chunk)


def capture(directory, after, cutoff, max_pages=10000, *, byte_budget=8*1024**3,
            bytes_per_second=1024**2, reserve_bytes=40*1024**3):
    if max_pages<1 or bytes_per_second<1 or reserve_bytes<0:
        raise ValueError('Invalid capture resource bounds')
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    config={'after':after,'cutoff':cutoff}
    path=directory/'config.json'
    if path.exists():
        if json.loads(path.read_text()) != config:raise ValueError('Capture parameters changed')
    else:
        path.write_text(json.dumps(config)+'\n')
    budget=Budget(directory/'transfer-budget.db',byte_budget)
    cursor=after
    for index in range(max_pages):
        page=directory/f'{index:06}.json'
        if page.exists():
            saved=json.loads(page.read_text())
            if saved['after'] != cursor:raise ValueError('Saved cursor chain broken')
            encoded=json.dumps(saved['response'],sort_keys=True).encode()
            if hashlib.sha256(encoded).hexdigest() != saved['sha256']:raise ValueError('Saved page checksum mismatch')
            result=page_result(saved['response'],cursor,cutoff)
        else:
            url='https://plc.directory/export?'+urllib.parse.urlencode({'count':1000,'after':cursor})
            if shutil.disk_usage(directory).free < reserve_bytes+32*1024*1024:
                raise RuntimeError('PLC tail disk reserve reached')
            payload=fetch_page(url,budget,bytes_per_second)
            records=[json.loads(line) for line in payload.decode().splitlines() if line]
            result=page_result(records,cursor,cutoff)
            encoded=json.dumps(records,sort_keys=True).encode()
            saved={'after':cursor,'fetched_at':time.time(),'response':records,'sha256':hashlib.sha256(encoded).hexdigest()}
            temporary=page.with_suffix('.part');temporary.write_text(json.dumps(saved)+'\n');temporary.replace(page)
            print(json.dumps({'page':index,'records':len(records),'through':records[-1]['createdAt'],'complete':result['complete']}),flush=True)
            time.sleep(1)
        if result['complete']:
            (directory/'complete.json').write_text(json.dumps({**config,'pages':index+1,'apply_ready':False})+'\n')
            return
        cursor=result['next_after']
    raise ValueError('PLC page budget exhausted; capture incomplete')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path);parser.add_argument('--after',required=True);parser.add_argument('--cutoff',required=True)
    parser.add_argument('--byte-budget',type=int,default=8*1024**3)
    parser.add_argument('--bytes-per-second',type=int,default=1024**2)
    parser.add_argument('--reserve-bytes',type=int,default=40*1024**3)
    args=parser.parse_args()
    if timestamp(args.cutoff) > datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=10):
        raise ValueError('Cutoff must be at least ten minutes old')
    if timestamp(args.after)>=timestamp(args.cutoff):raise ValueError('Invalid capture interval')
    capture(args.directory,args.after,args.cutoff,byte_budget=args.byte_budget,
            bytes_per_second=args.bytes_per_second,reserve_bytes=args.reserve_bytes)

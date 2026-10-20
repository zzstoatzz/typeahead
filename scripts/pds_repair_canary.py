#!/usr/bin/env python3
"""At most five authoritative PDS repairs, with health gates and durable receipts."""
import argparse
import fcntl
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pds_coverage import pds_from_document
from pds_repair_health import ENDPOINTS, blockers, fetch_health, error_details, search_lag
from pds_repair_ledger import Ledger


def baseline_budget(path):
    rows=[json.loads(line) for line in Path(path).read_text().splitlines() if line]
    if len(rows)<70 or rows[-1]['at']-rows[0]['at']<1000:
        raise ValueError('At least 70 health samples over 1000 seconds are required')
    if time.time()-rows[-1]['at']>120:
        raise ValueError('Baseline is stale')
    queues=[];lags=[]
    previous=None
    for row in rows:
        observed_at=row.get('finished_at',row['at']+sum(row.get(k,{}).get('seconds',0) for k in ENDPOINTS))
        reasons=blockers(row,previous,max_queue=100,max_write_age=15,max_search_lag=360,now=observed_at)
        if reasons:raise ValueError('Unhealthy baseline: '+', '.join(reasons))
        c=row['ingest']['body']['ingest']
        queues.extend([c['queued'],c['deletes_queued']]);lags.append(search_lag(row,observed_at))
        previous=row
    if max(queues)>100 or max(lags)>360:raise ValueError('Baseline exceeds canary safety ceiling')
    return dict(max_queue=max(30,max(queues)),max_write_age=15,max_search_lag=max(330,max(lags)+15)),rows[-1]


class Turso:
    def __init__(self,credentials):
        self.url=credentials['url'].replace('libsql://','https://').rstrip('/')+'/v2/pipeline'
        if urllib.parse.urlsplit(self.url).scheme!='https':raise ValueError('Turso requires HTTPS')
        self.token=credentials['token']

    def execute(self,sql,params):
        args=[{'name':key,'value':{'type':'integer' if isinstance(value,int) else 'text','value':str(value)}} for key,value in params.items()]
        body={'requests':[{'type':'execute','stmt':{'sql':sql,'named_args':args}},{'type':'close'}]}
        request=urllib.request.Request(self.url,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=5) as response:data=json.load(response)
        result=data['results'][0]
        if result['type']!='ok':
            raise RuntimeError('Turso statement failed: '+str(result.get('error',{}).get('code','unknown')))
        return [[int(c['value']) if c['type']=='integer' else c.get('value') for c in row] for row in result['response']['result']['rows']]


def health(previous,budget):
    result={'at':time.time(),'errors':{}}
    if 'search_progress' in previous:
        try:
            from pds_fly_progress import fetch_progress
            result['search_progress']=fetch_progress()
        except Exception as e:result['errors']['search_progress']=error_details(e)
    for name,url in ENDPOINTS.items():
        try:result[name]=fetch_health(url)
        except Exception as e:result['errors'][name]=error_details(e)
    reasons=blockers(result,previous,**budget)
    if reasons:raise RuntimeError('Health gate: '+', '.join(reasons))
    return result


def run(candidates,baseline,output,credentials,apply=False,max_rows=5):
    if not 1<=max_rows<=5:raise ValueError('Canary is limited to five accounts')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with (output/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (output/'events.jsonl').exists():raise ValueError('Existing canary requires explicit reconciliation; refusing restart')
        budget,previous=baseline_budget(baseline)
        client=Turso(credentials);ledger=Ledger(output/'ledger.db')
        with (output/'events.jsonl').open('x') as log:
            def event(**values):
                log.write(json.dumps({'at':time.time(),**values})+'\n');log.flush()
            event(event='start',apply=apply,budget=budget)
            attempted=0
            for item in candidates:
                if attempted>=max_rows:break
                if (output/'STOP').exists():raise RuntimeError('Stop file present')
                did=item['did'] if isinstance(item,dict) else item[0]
                if not re.fullmatch(r'did:plc:[a-z2-7]{24}',did):
                    event(event='skip',did=did,reason='requires separate did:web or invalid-DID handling');continue
                previous=health(previous,budget)
                current=client.execute('SELECT did,pds,updated_at FROM actors WHERE did=:did',{'did':did})
                if not current or current[0][1]!='':
                    event(event='skip',did=did,reason='deleted or already has PDS');continue
                with urllib.request.urlopen('https://plc.directory/'+did,timeout=10) as response:document=json.load(response)
                pds=pds_from_document(did,document)
                event(event='resolved',did=did,document=document,observed_updated_at=current[0][2])
                previous=health(previous,budget)
                if (output/'STOP').exists():raise RuntimeError('Stop file present')
                start=time.monotonic()
                if apply:
                    attempt=ledger.prepare(did,current[0][2],pds)
                    outcome=ledger.send(attempt,client.execute)
                    event(event='write',attempt=attempt,outcome=outcome,seconds=time.monotonic()-start,receipt=ledger.get(attempt))
                else:event(event='would_write',did=did,pds=pds)
                attempted+=1
                write_seconds=time.monotonic()-start
                previous=health(previous,budget)
                event(event='post_health',health=previous)
                if write_seconds>1:raise RuntimeError('Write exceeded one-second canary budget')
                if attempted<max_rows:time.sleep(60)
            event(event='complete',attempted=attempted)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidates',type=Path);parser.add_argument('baseline',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--max-rows',type=int,default=5)
    args=parser.parse_args()
    # Credentials arrive through stdin, never process arguments or a persisted file.
    credentials=json.load(sys.stdin)
    run(json.loads(args.candidates.read_text()),args.baseline,args.output,credentials,args.apply,args.max_rows)

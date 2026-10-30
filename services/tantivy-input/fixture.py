#!/usr/bin/env python3
"""Emit a tiny synthetic import stream and expected smoke cases, on the cloud VM."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

def line(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
def fixture(build_id,watermark):
    rows=[]
    def actor(suffix,handle,display,score,pds='https://local.test',hidden=False,deleted=False):
        did='did:example:fixture-'+suffix
        rows.append({'type':'actor','did':did,'handle':handle,'display_name':display,'quality_score':score,'pds':pds,'hidden':hidden,'deleted':deleted,'version':watermark,'avatar_url':'bafkfixtureavatar','labels':[{'val':'dev-fixture'}],'associated':{'chat':{'allowIncoming':'all'}},'created_at':'2026-10-28T00:00:00Z'})
        return did
    exact=actor('exact','alice.fixture.test','Alice Exact',0,'https://remote.test')
    prefix=actor('local-prefix','alice.fixture.test.extra','Alice Local',10000)
    local=actor('local-joint','justin.local.test','Justin Fa',100)
    remote=actor('remote-joint','justin.remote.test','Justin Far',200,'https://remote.test')
    actor('hidden','hidden.fixture.test','Justin Fa',100000,hidden=True)
    actor('deleted','deleted.fixture.test','Justin Fa',100001,deleted=True)
    actor('unicode','unicode.fixture.test','東京 太郎',50)
    actor('long','abcdefghijklmnopneedle.fixture.test','Long Display Token',25)
    rows.sort(key=lambda a:a['did'].encode())
    payload=[line(a) for a in rows];sha=hashlib.sha256(b''.join(payload)).hexdigest()
    manifest={'type':'manifest','schema_version':1,'build_id':build_id,'source_watermark':watermark,'watermark':str(watermark),'actor_count':len(rows),'source_actor_count':len(rows),'normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_format':'synthetic-operational-fixture','synthetic':True,'partial':False}
    footer={'type':'end','build_id':build_id,'actor_count':len(rows),'actors_sha256':sha}
    expected={'build_id':build_id,'synthetic':True,'actor_count':len(rows),'actors_sha256':sha,'queries':[
        {'id':'exact-priority','q':'alice.fixture.test','pds':None,'expected_ids':[exact,prefix]},
        {'id':'joint-global','q':'Justin Fa','pds':None,'expected_ids':[remote,local]},
        {'id':'joint-reversed','q':'Fa Justin','pds':None,'expected_ids':[remote,local]},
        {'id':'joint-local','q':'Justin Fa','pds':'local.test','expected_ids':[local]},
        {'id':'joint-remote','q':'Justin Fa','pds':'remote.test','expected_ids':[remote]},
        {'id':'valid-empty-scope','q':'Justin Fa','pds':'empty.test','expected_ids':[]},
        {'id':'long-handle','q':'abcdefghijklmnopneedle','pds':None,'expected_ids':['did:example:fixture-long']}],
        'preference_queries':[
            {'id':'prefer-local','q':'Justin Fa','prefer_pds':'local.test','preference':'pds_first','expected_ids':[local,remote]},
            {'id':'boost-local','q':'Justin Fa','prefer_pds':'local.test','preference':'boost','boost':1000,'expected_ids':[local,remote]}],
        'hydration_by_did':{exact:{'did':exact,'handle':'alice.fixture.test','displayName':'Alice Exact','labels':[{'val':'dev-fixture'}],'associated':{'chat':{'allowIncoming':'all'}},'createdAt':'2026-10-28T00:00:00Z','avatar':f'https://cdn.bsky.app/img/avatar/plain/{exact}/bafkfixtureavatar','avatarFallback':f'https://remote.test/xrpc/com.atproto.sync.getBlob?did={exact}&cid=bafkfixtureavatar'}}}
    return line(manifest)+b''.join(payload)+line(footer),expected

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--build-id',default='fixture-tantivy-dev-20261028');p.add_argument('--watermark',type=int,default=1793230000);p.add_argument('--output',required=True);p.add_argument('--expected',required=True);a=p.parse_args()
    if not re.fullmatch('fixture-[A-Za-z0-9._-]{1,100}',a.build_id) or not 0<a.watermark<2**63:raise ValueError('fixture identity/watermark invalid')
    payload,expected=fixture(a.build_id,a.watermark)
    with Path(a.expected).open('xb') as f:f.write(line(expected))
    if a.output=='-':sys.stdout.buffer.write(payload)
    else:
        with Path(a.output).open('xb') as f:f.write(payload)
if __name__=='__main__':main()

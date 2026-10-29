from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
BIN=ROOT/'target/release/tantivy-raw-oracle'
POLICY=REPO/'src/identity-quarantine.json'

def command(*args,check=True,input=None):
    return subprocess.run([str(BIN),*map(str,args)],input=input,text=True,capture_output=True,check=check,timeout=60)

def actor(n):
    displays=['Justin Fagnani','Éric Alpha Music','東京太郎','日本 東京 本太','Alice 日','one two three four five six seven eight nine Needle','Longabcdefghijklmnopqrstuv Person','Delta Beta']
    return {'did':f'did:plc:synthetic{n:07}','handle':'zzstoatzz.io' if n==0 else f'person{n:06}.bsky.social','display_name':displays[n%len(displays)],'quality_score':n%311-100,'pds':'https://selfhosted.test' if n%3==0 else '', 'hidden':n>0 and n%97==0,'deleted':n>0 and n%251==0,'version':1000,'labels':[],'associated':{}}

def corpus(path,rows):
    lines=[json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()+b'\n' for row in rows]
    checksum=hashlib.sha256(b''.join(lines)).hexdigest()
    header={'type':'manifest','build_id':'b1793290000-oracle','source_watermark':1000,'actor_count':len(rows),'source_actor_count':len(rows),'partial':False,'normalize_version':'v2-cjk-bigram'}
    footer={'type':'end','build_id':header['build_id'],'actor_count':len(rows),'actors_sha256':checksum}
    path.write_bytes(json.dumps(header).encode()+b'\n'+b''.join(lines)+json.dumps(footer).encode()+b'\n')
    return header

def cases(path,values):
    rows=[]
    for i,value in enumerate(values):rows.append({'id':str(i),'limit':10,'category':'independent-adversarial','native_limitations':['known capped native retrieval is not the truth oracle'],**value})
    path.write_text(json.dumps({'schema_version':1,'source_kind':'frozen-materialized-hidden','cases':rows}))

def run(path,queries,output,feed=None):
    args=['run','--input',path,'--queries',queries,'--output',output,'--identity-policy',POLICY,'--source-kind','frozen-materialized-hidden','--max-seconds','60','--max-rss-mb','256']
    if feed:args+=['--updates',feed]
    return command(*args)


class OracleTests(unittest.TestCase):
    def test_independent_raw_adversarial_matching_ranking_and_checkpoint_updates(self):
        with tempfile.TemporaryDirectory()as temporary:
            root=Path(temporary);rows=[actor(i)for i in range(24)]
            for row in rows[12:]:row['display_name']='Neutral Individual'
            rows[0].update(handle='exact.test',display_name='Justin Fagnani',quality_score=1,pds='https://local.test')
            rows[1].update(handle='exact.test.long',display_name='Justin Other',quality_score=1e30,pds='https://local.test')
            rows[2].update(display_name='Other Fagnani',quality_score=1e30)
            rows[3].update(display_name='日本 東京 本太',quality_score=1e30)
            rows[4].update(display_name='名前日本太郎',quality_score=-100)
            rows[5].update(display_name='Needle Boost',quality_score=2,pds='https://remote.test')
            rows[6].update(display_name='Needle Boost',quality_score=2,pds='https://local.test')
            rows[7].update(display_name='Needle Boost',quality_score=1,pds='https://local.test')
            rows[8].update(display_name='one two three four five six seven eight nine RareTerm')
            rows[9].update(display_name='Longabcdefghijklmnopqrstuv Unique')
            rows[10].update(display_name='Justin Fagnani',hidden=True,quality_score=1e30)
            rows[11].update(handle='DONALD.TRUMP.COM',display_name='Justin Fagnani',quality_score=1e30)
            input=root/'raw.jsonl';header=corpus(input,rows);queries=root/'queries.json'
            values=[{'q':'exact.test'},{'q':'Justin Fa'},{'q':'Fa Justin'},{'q':'日本太'},{'q':'RareT'},{'q':'longabcdefghijklmnopqrst'},{'q':'Needle Boost','prefer_pds':'local.test','preference':'boost','boost':2**-149,'limit':1},{'q':'Needle Boost','prefer_pds':'local.test','preference':'boost','boost':1e30,'limit':100},{'q':'Needle Boost','pds':'remote.test'},{'q':'Needle Boost','prefer_pds':'local.test','preference':'pds_first'},{'q':'東京'},{'q':'Greek λ'},{'q':'Needle Boost','pds':''}]
            cases(queries,values)
            changed=dict(rows[0],display_name='Renamed Entirely',version=1001)
            hidden=dict(changed,hidden=True,version=1002)
            restored=dict(rows[0],version=1003)
            feed={'schema_version':1,'build_id':header['build_id'],'source_watermark':1000,'source_kind':'frozen-materialized-hidden','baseline_actors':[rows[0]],'batches':[{'batch_id':'rename','cursor':1,'expected_cursor':0,'actors':[changed]},{'batch_id':'hide','cursor':2,'expected_cursor':1,'actors':[hidden]},{'batch_id':'restore','cursor':3,'expected_cursor':2,'actors':[restored]}]}
            feed_path=root/'updates.json';feed_path.write_text(json.dumps(feed,separators=(',',':')))
            output=root/'oracle.json';run(input,queries,output,feed_path);result=json.loads(output.read_text());base=result['checkpoints'][0]['queries']
            self.assertEqual(base['0']['ids'][0],rows[0]['did'])
            self.assertEqual(base['1']['ids'],[rows[0]['did']]);self.assertEqual(base['2']['ids'],[rows[0]['did']])
            self.assertEqual(base['3']['ids'],[rows[4]['did']])
            self.assertEqual(base['4']['ids'],[rows[8]['did']]);self.assertEqual(base['5']['ids'],[rows[9]['did']])
            self.assertEqual(base['6']['ids'],[rows[6]['did']]);self.assertEqual(base['7']['ids'][:3],[rows[6]['did'],rows[7]['did'],rows[5]['did']])
            self.assertEqual(base['8']['ids'],[rows[5]['did']]);self.assertEqual(base['9']['ids'][:3],[rows[6]['did'],rows[7]['did'],rows[5]['did']])
            self.assertIn(rows[3]['did'],base['10']['ids']);self.assertEqual(base['11']['status'],400);self.assertEqual(base['12']['status'],400)
            self.assertEqual(result['checkpoints'][1]['queries']['1']['ids'],[])
            self.assertNotIn(rows[0]['did'],result['checkpoints'][2]['queries']['0']['ids'])
            self.assertEqual(result['checkpoints'][3]['queries'],base)
            self.assertEqual(result['receipt']['updates_sha256'],hashlib.sha256(feed_path.read_bytes()).hexdigest())
            self.assertFalse(result['receipt']['expected_ids_derived_from_engine_responses'])

    def test_complete_lifecycle_refills_after_all_initial_winners_are_removed(self):
        with tempfile.TemporaryDirectory()as temporary:
            root=Path(temporary);rows=[actor(i)for i in range(80)]
            for i,row in enumerate(rows):row.update(handle=f'person{i:06}.test',display_name='Needle Person',quality_score=80-i,hidden=False,deleted=False,pds='https://local.test')
            input=root/'raw.jsonl';header=corpus(input,rows);queries=root/'q.json'
            definitions=[{'q':'Needle','limit':10},{'q':'Renamed','limit':10},{'q':'Needle','pds':'local.test','limit':10},{'q':'Needle','pds':'moved.test','limit':10},{'q':'Needle','prefer_pds':'moved.test','preference':'boost','boost':1000,'limit':10}]
            cases(queries,definitions);affected=[dict(row)for row in rows[:32]];states=[affected];batches=[]
            for cursor,stage in enumerate(['rename','rescore','metadata','hide','show','delete','restore-deleted','pds-move','restore-baseline'],1):
                current=[dict(row,version=1000+cursor)for row in states[-1]]
                for i,row in enumerate(current):
                    if stage=='rename':row['display_name']='Renamed Person'
                    if stage=='rescore':row['quality_score']=i-100
                    if stage=='metadata':row['avatar_url']='https://example.test/image'
                    if stage=='hide':row['hidden']=True
                    if stage=='show':row['hidden']=False
                    if stage=='delete':row['deleted']=True
                    if stage=='restore-deleted':row['deleted']=False
                    if stage=='pds-move':row['pds']='https://moved.test'
                    if stage=='restore-baseline':row.update(affected[i],version=1000+cursor)
                states.append(current);batches.append({'batch_id':stage,'cursor':cursor,'expected_cursor':cursor-1,'actors':current})
            feed={'schema_version':1,'build_id':header['build_id'],'source_watermark':1000,'source_kind':'frozen-materialized-hidden','baseline_actors':affected,'batches':batches,'replay_batch_ids':['rename','restore-baseline']}
            feed_path=root/'updates.json';feed_path.write_text(json.dumps(feed));output=root/'oracle.json';run(input,queries,output,feed_path)
            result=json.loads(output.read_text());self.assertEqual(len(result['checkpoints']),10)
            for cursor,state in enumerate(states):
                for i,query in enumerate(definitions):
                    candidates=[]
                    for row in state+rows[32:]:
                        if row['hidden']or row['deleted']or not row['display_name'].lower().startswith(query['q'].lower()):continue
                        host=row['pds'].removeprefix('https://')
                        if query.get('pds')and query['pds']!=host:continue
                        score=Fraction(row['quality_score'])+Fraction(query.get('boost',0)if host==query.get('prefer_pds')else 0)
                        candidates.append((-score,row['did']))
                    expected=[did for _,did in sorted(candidates)[:query['limit']]]
                    self.assertEqual(result['checkpoints'][cursor]['queries'][str(i)]['ids'],expected,(cursor,i))
            self.assertEqual(result['checkpoints'][4]['queries']['0']['ids'],[row['did']for row in rows[32:42]])
            roster=root/'scopes.json';roster.write_text(json.dumps({'schema_version':1,'build_id':header['build_id'],'source_watermark':1000,'hosts':['local.test']}))
            frozen=root/'frozen.json';command('freeze','--input',input,'--output',frozen,'--identity-policy',POLICY,'--cases','64','--sample-actors','80','--updates',feed_path,'--native-scopes',roster)
            frozen_cases=json.loads(frozen.read_text());self.assertEqual(frozen_cases['sample']['updates_sha256'],hashlib.sha256(feed_path.read_bytes()).hexdigest())
            self.assertTrue(any(c['category']=='feed-conjunction'for c in frozen_cases['cases']))
            self.assertTrue(any(c.get('pds')=='local.test'and c['native_shared_features']for c in frozen_cases['cases']))
            self.assertTrue(all(not c['native_shared_features']for c in frozen_cases['cases']if c.get('prefer_pds')))
            run(input,frozen,root/'frozen-oracle.json',feed_path)


    def test_exact_integer_score_matches_python_fraction(self):
        rng=random.Random(314159)
        def as_float(bits):return struct.unpack('<f',struct.pack('<I',bits))[0]
        pairs=[[0x3f800000,0x7149f2ca,0x40000000,0x7149f2ca],[0x40000000,1,0x40000000,0],[0xff7fffff,0x7f7fffff,0,0]]
        while len(pairs)<128:
            values=[rng.getrandbits(32)for _ in range(4)]
            if all(math.isfinite(as_float(v))for v in values):pairs.append(values)
        for values in pairs:
            a,b,c,d=map(lambda v:Fraction.from_float(as_float(v)),values);expected=(a+b>c+d)-(a+b<c+d)
            result=command('score',*[part for key,value in zip(['a','b','c','d'],values)for part in ['--'+key,str(value)]])
            self.assertEqual(json.loads(result.stderr)['cmp'],expected,values)

    def test_projection_parity_with_canonical_actor_api_and_budget_edges(self):
        examples=[('ALICE.Example','Éric Ａｌｉｃｅ 東京太郎'),('alice.test','one two three four five six seven eight nine'),('a'*256,''),('a'*257,''),('valid.test','é'*160),('valid.test','a'*256),('valid.test','a'*257),('a.'*126+'a',''),('valid.test','が カタカナ ﾄｳｷｮｳ'),('valid.test','Greek λ Arabic عربي'),('x'*1025,'')]
        payload=''.join(json.dumps({'handle':h,'display':d},ensure_ascii=False)+'\n'for h,d in examples)
        projected=[json.loads(line)for line in command('project',input=payload).stdout.splitlines()]
        raw=''.join(json.dumps({'op':'actor','handle':h,'display':d},ensure_ascii=False)+'\n'for h,d in examples)
        canonical=[json.loads(line)for line in command('canonical',input=raw).stdout.splitlines()]
        for (handle,display),actual,expected in zip(examples,projected,canonical):
            self.assertEqual(actual['status']=='ok',expected['status']=='ok',(handle[:20],display[:20]))
            if expected['status']=='ok':self.assertEqual((actual['normalized_handle'],actual['normalized_display']),(expected['normalized_handle'],expected['normalized_display']))

    def test_stream_proof_rejects_tampering_and_never_publishes_partial_answers(self):
        with tempfile.TemporaryDirectory()as temporary:
            root=Path(temporary);input=root/'raw.jsonl';corpus(input,[actor(i)for i in range(10)]);queries=root/'q.json';cases(queries,[{'q':'person'}])
            content=input.read_bytes();input.write_bytes(content.replace(b'Justin',b'Jastin',1));output=root/'bad.json'
            result=command('run','--input',input,'--queries',queries,'--output',output,'--identity-policy',POLICY,'--source-kind','frozen-materialized-hidden',check=False)
            self.assertNotEqual(result.returncode,0);self.assertFalse(output.exists());self.assertFalse(list(root.glob('*.partial-*')))
            streamed=command('run','--input','-','--queries',queries,'--output',root/'stdin.json','--identity-policy',POLICY,'--source-kind','frozen-materialized-hidden',input=content.decode())
            self.assertEqual(json.loads(streamed.stderr)['actors_checked'],10)


    def test_bounded_10000_actor_512_query_pilot(self):
        with tempfile.TemporaryDirectory()as temporary:
            root=Path(temporary);input=root/'raw.jsonl';corpus(input,[actor(i)for i in range(10000)]);queries=root/'queries.json'
            command('freeze','--input',input,'--output',queries,'--identity-policy',POLICY,'--cases','512','--sample-actors','10000')
            start=time.perf_counter();result=run(input,queries,root/'oracle.json');report=json.loads(result.stderr)
            self.assertEqual(report['actors_checked'],10000);self.assertEqual(report['cases'],512);self.assertLess(report['peak_rss_mb'],256)
            print('ORACLE_SYNTHETIC_PILOT '+json.dumps({'actors':10000,'queries':512,'seconds':time.perf_counter()-start,'runner':report,'full_corpus_or_cloud_claim':False}))
            altered=json.loads(queries.read_text());altered['sample']['sample_prefix_sha256']='0'*64;queries.write_text(json.dumps(altered))
            rejected=command('run','--input',input,'--queries',queries,'--output',root/'wrong-sample.json','--identity-policy',POLICY,'--source-kind','frozen-materialized-hidden',check=False)
            self.assertNotEqual(rejected.returncode,0);self.assertIn('sample prefix hash',rejected.stderr);self.assertFalse((root/'wrong-sample.json').exists())


if __name__=='__main__':unittest.main()

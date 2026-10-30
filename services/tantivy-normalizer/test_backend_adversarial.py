import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent
BIN=Path(os.environ.get('TANTIVY_TEST_BIN',str(ROOT.parent/'tantivy/target/release/typeahead-tantivy')))
TEST_SECRET='isolated-adversarial-fixture-value'

def actor(did,version,**fields):
    value={'did':'did:fixture:'+did,'handle':did+'.fixture.test','display_name':'Adversarial Person',
           'quality_score':1.0,'version':version,'pds':'https://local.test','labels':[],
           'associated':{},'avatar_url':'','hidden':False,'deleted':False}
    value.update(fields)
    return value

def seed(path,actors,watermark,partial=False):
    rows=[json.dumps(a,ensure_ascii=False,separators=(',',':')).encode()+b'\n'
          for a in sorted(actors,key=lambda a:a['did'])]
    source_count=len(actors)*2 if partial else len(actors)
    header={'type':'manifest','schema_version':1,'build_id':'adversarial-fixture',
            'source_watermark':watermark,'actor_count':len(actors),'source_actor_count':source_count,
            'normalize_version':'v2-cjk-bigram','source_format':'FLATPX05/actor-v1','partial':partial}
    footer={'type':'end','build_id':header['build_id'],'actor_count':len(actors),
            'actors_sha256':hashlib.sha256(b''.join(rows)).hexdigest()}
    path.write_bytes(json.dumps(header).encode()+b'\n'+b''.join(rows)+json.dumps(footer).encode()+b'\n')

def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));return listener.getsockname()[1]

def request(base,path,payload=None,authorized=False):
    headers={}
    if payload is not None:headers['Content-Type']='application/json'
    if authorized:headers['Authorization']='Bearer '+TEST_SECRET
    req=urllib.request.Request(base+path,headers=headers,
        data=None if payload is None else json.dumps(payload,separators=(',',':')).encode())
    try:response=urllib.request.urlopen(req,timeout=5)
    except urllib.error.HTTPError as error:response=error
    with response:return response.status,json.load(response)

def stop(child):
    if child.poll() is None:
        os.killpg(child.pid,signal.SIGTERM)
        try:child.wait(timeout=3)
        except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)

class BackendAdversarial(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='.test-http-',dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def import_seed(self,name,actors,watermark,partial=False):
        path=self.root/(name+'.jsonl');data=self.root/name;seed(path,actors,watermark,partial)
        result=subprocess.run([str(BIN),'import','--data',str(data),'--input',str(path),'--batch-size','1'],
                              capture_output=True,text=True,timeout=25)
        return data,result

    def start(self,data):
        public,admin=free_port(),free_port()
        while admin==public:admin=free_port()
        log=(self.root/(data.name+'-server.log')).open('ab');self.addCleanup(log.close)
        child=subprocess.Popen([str(BIN),'serve','--data',str(data),'--listen',f'127.0.0.1:{public}',
                                '--admin-listen',f'127.0.0.1:{admin}'],
                               env=dict(os.environ,ADMIN_SECRET=TEST_SECRET),stdout=log,stderr=log,start_new_session=True)
        self.addCleanup(stop,child)
        base=f'http://127.0.0.1:{public}';private=f'http://127.0.0.1:{admin}'
        for _ in range(100):
            if child.poll() is not None:self.fail('backend exited during startup')
            try:
                if request(base,'/index/status')[0]==200:return child,base,private
            except OSError:pass
            time.sleep(.03)
        self.fail('backend never exposed readable index status')

    def test_stale_default_is_unready_but_replay_can_recover(self):
        old=int(time.time())-10000
        data,result=self.import_seed('stale',[actor('stale',old)],old)
        self.assertEqual(result.returncode,0,result.stderr)
        _,base,admin=self.start(data)
        status,metadata=request(base,'/index/status');self.assertEqual(status,200)
        self.assertEqual(metadata['watermark'],old)
        self.assertEqual(request(base,'/ready')[0],503)
        self.assertEqual(request(base,'/search?q=stale.fixture.test')[0],503)
        current=int(time.time())
        status,receipt=request(admin,'/admin/apply',{'actors':[],'watermark':current,
            'source_cursor':{'audit':'caught-up'},'expected_source_cursor':None},True)
        self.assertEqual(status,200,receipt)
        self.assertEqual(receipt['watermark'],current)
        self.assertEqual(request(base,'/ready')[0],200)
        status,response=request(base,'/search?q=stale.fixture.test')
        self.assertEqual(status,200,response)
        self.assertEqual(response['actors'][0]['did'],'did:fixture:stale')

    def test_oversized_search_projection_never_becomes_partial_ready(self):
        now=int(time.time())
        rows=[actor('first',now),actor('second',now,display_name='a'*257)]
        data,result=self.import_seed('oversized',rows,now)
        self.assertNotEqual(result.returncode,0,'oversized projection silently admitted')
        status=subprocess.run([str(BIN),'status','--data',str(data)],capture_output=True,text=True,timeout=10)
        self.assertEqual(status.returncode,0,status.stderr)
        self.assertFalse(json.loads(status.stdout)['import_complete'])
        _,base,_=self.start(data)
        self.assertEqual(request(base,'/ready')[0],503)
        self.assertEqual(request(base,'/search?q=first.fixture.test')[0],503)

    def test_partial_export_is_not_full_ready_by_default(self):
        now=int(time.time())
        _,result=self.import_seed('partial',[actor('partial',now)],now,partial=True)
        self.assertNotEqual(result.returncode,0,'partial source export silently qualifies as complete')

    def test_u16_opaque_avatar_survives_import_and_public_hydration(self):
        now=int(time.time())
        avatar='https://avatar.fixture.test/'+('x'*(65535-len('https://avatar.fixture.test/')))
        data,result=self.import_seed('avatar',[actor('avatar',now,avatar_url=avatar)],now)
        self.assertEqual(result.returncode,0,result.stderr)
        _,base,_=self.start(data)
        status,response=request(base,'/search?q=avatar.fixture.test')
        self.assertEqual(status,200,response)
        self.assertEqual(response['actors'][0]['avatarFallback'],avatar)
        self.assertIsNone(response['actors'][0]['avatar'])

    def test_invalid_batch_is_atomic_with_cursor_and_snapshot_barriers(self):
        now=int(time.time())
        original=actor('atomic',now)
        data,result=self.import_seed('atomic',[original],now)
        self.assertEqual(result.returncode,0,result.stderr)
        _,base,admin=self.start(data)
        changed=dict(original,display_name='Should Not Appear',version=now+1)
        invalid=actor('invalid',now+1,display_name='a'*257)
        code,_=request(admin,'/admin/apply',{'actors':[changed,invalid],
            'source_cursor':{'audit':'bad-batch'},'expected_source_cursor':None},True)
        self.assertEqual(code,400)
        self.assertIsNone(request(base,'/index/status')[1]['source_cursor'])
        status,response=request(base,'/search?q=atomic.fixture.test')
        self.assertEqual(status,200,response)
        self.assertEqual(response['actors'][0]['displayName'],original['display_name'])
        old_absent=actor('never-in-snapshot',now,display_name='Phantom Insert')
        code,receipt=request(admin,'/admin/apply',{'actors':[old_absent]},True)
        self.assertEqual(code,200,receipt);self.assertEqual(receipt['stale'],1)
        self.assertFalse(request(base,'/search?q=never-in-snapshot.fixture.test')[1]['actors'])

if __name__=='__main__':unittest.main()

import concurrent.futures,copy,hashlib,json,os,resource,secrets,signal,socket,subprocess,tempfile,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BIN=Path(os.environ.get('TA_BINARY',str(ROOT/'target/release/typeahead-tantivy')))
def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def actor(n,handle,display,score=1,pds='https://local.test',hidden=False):
 return {'did':f'did:plc:test{n:04}','handle':handle,'display_name':display,'quality_score':score,'pds':pds,'hidden':hidden,'deleted':False,'version':100,'labels':[],'associated':{},'avatar_url':'bafkfixture','created_at':'2026-10-28T00:00:00Z'}
def write_seed(path,actors,bad_footer=False,omit_footer=False):
 lines=[json.dumps(a,ensure_ascii=False,separators=(',',':')).encode()+b'\n' for a in sorted(actors,key=lambda a:a['did'])]
 header={'type':'manifest','build_id':'b1793236000-pilot','source_watermark':100,'actor_count':len(actors),'normalize_version':'v2-cjk-bigram','partial':True,'source_actor_count':len(actors)}
 footer={'type':'end','build_id':header['build_id'],'actor_count':len(actors),'actors_sha256':'bad' if bad_footer else hashlib.sha256(b''.join(lines)).hexdigest()}
 path.write_bytes(json.dumps(header).encode()+b'\n'+b''.join(lines)+(b'' if omit_footer else json.dumps(footer).encode()+b'\n'))
def request(base,path,body=None,secret=None):
 headers={}
 if secret:headers['Authorization']='Bearer '+secret
 if body is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
 try:r=urllib.request.urlopen(req,timeout=10)
 except urllib.error.HTTPError as e:r=e
 with r:return r.status,dict(r.headers),json.load(r)
def query(base,q,**params):
 values={'q':q,**params};status,headers,data=request(base,'/search?'+urllib.parse.urlencode(values));assert status==200,(status,data)
 return data['actors'],headers
def stop(child):
 if child.poll() is None:
  child.send_signal(signal.SIGTERM)
  try:child.wait(timeout=5)
  except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
def start(data,log,secret,crash=None):
 public,admin=port(),port();env=dict(os.environ,ADMIN_SECRET=secret)
 if crash:env['TYPEAHEAD_TEST_CRASH']=crash
 else:env.pop('TYPEAHEAD_TEST_CRASH',None)
 child=subprocess.Popen([str(BIN),'serve','--data',str(data),'--allow-partial','true','--allow-stale','true',*(['--retrieval',os.environ['TA_RETRIEVAL_STRATEGY']] if 'TA_RETRIEVAL_STRATEGY' in os.environ else []),'--listen',f'127.0.0.1:{public}','--admin-listen',f'127.0.0.1:{admin}'],env=env,stdout=log,stderr=log,preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_CORE,(0,0)),start_new_session=True)
 base=f'http://127.0.0.1:{public}';private=f'http://127.0.0.1:{admin}'
 for _ in range(100):
  if child.poll() is not None:raise RuntimeError('server exited during startup')
  try:
   if request(base,'/health')[0]==200:return child,base,private
  except OSError:pass
  time.sleep(.05)
 stop(child);raise RuntimeError('server not ready')
def main():
 checks=[]
 with tempfile.TemporaryDirectory(prefix='typeahead-tantivy-http-') as temporary:
  root=Path(temporary);data=root/'data';seed=root/'seed.jsonl';secret=secrets.token_urlsafe(32)
  rows=[actor(1,'justin-other.test','Justin Other',9000),actor(2,'falcon.test','Other Fagnani',8000),actor(3,'joint.test','Justin Fagnani',1),actor(4,'alice.test','Alice Exact',0,'https://remote.test'),actor(5,'alice.test.long','Alice Prefix',100000),actor(6,'hidden.test','Veil Unique',9000,hidden=True),actor(7,'visible.test','Veil Unique',0),actor(8,'f32-a.test','Float Tie',16777216),actor(9,'f32-b.test','Float Tie',16777217),actor(10,'zero-a.test','Signed Zero',-0.0),actor(11,'zero-b.test','Signed Zero',0.0)]
  rows[6]['avatar_url']='https://avatar.test/'+('a'*9000)
  rows.extend([actor(12,'HANDLE.INVALID','Placeholder Safe',99999),actor(13,'placeholder-visible.test','Placeholder Safe',1)])
  rows.extend(actor(100+n,f'false{n}.test','日本 東京 本太',1000+n) for n in range(70));rows.append(actor(200,'real-japan.test','名前日本太郎',0))
  write_seed(seed,rows)
  imported=subprocess.run([str(BIN),'import','--data',str(data),'--input',str(seed),'--allow-partial','true','--batch-size','17'],text=True,capture_output=True,timeout=30)
  assert imported.returncode==0,imported.stderr
  metadata=json.loads(imported.stdout);assert metadata['import_complete'] and metadata['actor_count']==len(rows)
  checks.append('verified_manifest_footer_stream_import')
  with (root/'server.log').open('w') as log:
   child,base,admin=start(data,log,secret)
   try:
    expected_binary=hashlib.sha256(BIN.read_bytes()).hexdigest()
    expected_strategy=os.environ.get('TA_RETRIEVAL_STRATEGY','per-segment')
    for route in ['/search?q=Justin','/health','/ready','/index/status','/debug/search?q=Justin']:
     code,identity,body=request(base,route)
     assert code==200 and identity['x-typeahead-binary-sha256']==expected_binary and identity['x-typeahead-retrieval-strategy']==expected_strategy
     assert identity.get('x-typeahead-machine-id')==os.environ.get('FLY_MACHINE_ID')
     assert identity.get('x-typeahead-machine-version')==os.environ.get('FLY_MACHINE_VERSION')
     if route=='/index/status':assert body['binary_sha256']==expected_binary and body['retrieval_strategy']==expected_strategy and body['machine_id']==os.environ.get('FLY_MACHINE_ID') and body['instance_id']==os.environ.get('FLY_MACHINE_VERSION')
    checks.append('actual_runtime_binary_and_strategy_fingerprints_all_success_routes')
    answer,_=query(base,'Justin Fa');assert [a['did'] for a in answer]==[rows[2]['did']]
    reverse,_=query(base,'Fa Justin');assert reverse==answer
    checks.append('multiword_complete_intersection')
    for params in [{},{'pds':'local.test'},{'prefer_pds':'local.test','preference':'pds_first'}]:assert [a['did'] for a in query(base,'Placeholder',limit=1,**params)[0]]==['did:plc:test0013']
    assert not query(base,'HANDLE.INVALID')[0]
    checks.append('normalized_placeholder_exclusion_before_topk_scope_and_preference')
    answer,headers=query(base,'Justin Fa',pds='HTTPS://LOCAL.TEST./');assert answer and headers['x-typeahead-pds']=='local.test'
    answer,_=query(base,'Justin Fa',pds='empty.test');assert not answer
    checks.append('pds_before_selection_and_ack')
    for endpoint in ['/search','/xrpc/tech.waow.typeahead.searchActors','/xrpc/app.bsky.actor.searchActorsTypeahead']:
     for scope in ['pds','prefer_pds']:
      for invalid in ['', ' ', 'localhost', 'https://u:p@local.test', 'https://local.test/path']:
       status,_,body=request(base,endpoint+'?'+urllib.parse.urlencode({'q':'Justin',scope:invalid}))
       assert status==400,(endpoint,scope,invalid,status,body)
    checks.append('present_empty_and_invalid_scope_rejected_direct_and_both_aliases')

    answer,_=query(base,'alice.test',prefer_pds='local.test',preference='pds_first');assert answer[0]['did']==rows[3]['did']
    answer,_=query(base,'Float Tie');assert [a['did'] for a in answer]==[rows[7]['did'],rows[8]['did']]
    answer,_=query(base,'Signed Zero');assert [a['did'] for a in answer]==[rows[9]['did'],rows[10]['did']]
    checks.append('exact_priority_f32_rounding_and_zero_ties')
    answer,_=query(base,'日本太',limit=1);assert [a['did'] for a in answer]==[rows[-1]['did']]
    checks.append('cjk_contiguity_verification_refills_past_70_false_candidates')
    answer,_=query(base,'Veil Unique');assert [a['did'] for a in answer]==[rows[6]['did']]
    assert answer[0]['avatar'] is None and answer[0]['avatarFallback']==rows[6]['avatar_url']
    normal_avatar,_=query(base,'alice.test');assert normal_avatar[0]['avatar'].startswith('https://cdn.bsky.app/')
    checks.append('moderation_before_topk_and_profile_hydration')
    canonical=request(base,'/xrpc/tech.waow.typeahead.searchActors?q=Justin%20Fa')[2]
    alias=request(base,'/xrpc/app.bsky.actor.searchActorsTypeahead?q=Justin%20Fa')[2];assert canonical==alias
    assert request(base,'/admin/apply',{'actors':[]},secret)[0]==404
    assert request(admin,'/admin/apply',{'actors':[]})[0]==401
    assert request(base,'/search?q=a&pds=bad%40host.test')[0]==400
    assert request(base,'/search?q='+('a'*513))[0]==400
    checks.append('aliases_public_write_exclusion_auth_and_input_bounds')
    changed=copy.deepcopy(rows[2]);changed.update(version=101,display_name='Renamed User')
    receipt=request(admin,'/admin/apply',{'actors':[changed],'source_cursor':{'page':1},'expected_source_cursor':None},secret)
    assert receipt[0]==200,receipt
    assert receipt[2]['source_cursor']=={'page':1}
    assert not query(base,'Justin Fa')[0] and query(base,'Renamed User')[0]
    changed.update(version=102,pds='https://remote.test')
    assert request(admin,'/admin/apply',{'actors':[changed]},secret)[0]==200
    assert not query(base,'Renamed User',pds='local.test')[0] and query(base,'Renamed User',pds='remote.test')[0]
    tombstone=copy.deepcopy(changed);tombstone.update(version=103,deleted=True)
    assert request(admin,'/admin/apply',{'actors':[tombstone]},secret)[0]==200
    assert request(admin,'/admin/apply',{'actors':[changed]},secret)[2]['stale']==1
    checks.append('renames_pds_migration_tombstones_stale_rejection_and_cursor_atomicity')
   finally:stop(child)
   child,base,admin=start(data,log,secret)
   try:
    assert not query(base,'Renamed User')[0]
    assert request(admin,'/admin/apply',{'actors':[changed]},secret)[2]['stale']==1
    assert request(admin,'/admin/apply',{'actors':[tombstone]},secret)[2]['replayed']==1
    conflicting=copy.deepcopy(tombstone);conflicting['display_name']='Conflicting';assert request(admin,'/admin/apply',{'actors':[conflicting]},secret)[0]==409
    assert request(admin,'/admin/apply',{'actors':[],'source_cursor':{'page':3},'expected_source_cursor':{'page':0}},secret)[0]==409
    assert request(base,'/index/status')[2]['source_cursor']=={'page':1}
    checks.append('durable_restart_version_tombstone_and_cursor_barriers')
   finally:stop(child)
   resurrection=copy.deepcopy(changed);resurrection.update(version=104,display_name='Recovered User')
   for killpoint in ['before_commit','after_commit']:
    child,base,admin=start(data,log,secret,killpoint)
    try:
     try:request(admin,'/admin/apply',{'actors':[resurrection],'source_cursor':{'page':2},'expected_source_cursor':{'page':1}},secret)
     except (OSError,ValueError):pass
     child.wait(timeout=10)
    finally:stop(child)
    child,base,admin=start(data,log,secret)
    try:
     recovered=bool(query(base,'Recovered User')[0]);assert recovered==(killpoint=='after_commit')
     cursor=request(base,'/index/status')[2]['source_cursor'];assert cursor=={'page':2 if recovered else 1}
    finally:stop(child)
   checks.append('crash_before_commit_rolls_back_after_commit_recovers_profile_and_cursor')
   child,base,admin=start(data,log,secret)
   try:
    stable=actor(300,'stable.test','Version 105',1);stable.update(version=105,labels=[{'val':'v105'}]);assert request(admin,'/admin/apply',{'actors':[stable]},secret)[0]==200
    def update():
     for version in range(106,116):
      a=copy.deepcopy(stable);a.update(version=version,display_name=f'Version {version}',labels=[{'val':f'v{version}'}]);assert request(admin,'/admin/apply',{'actors':[a]},secret)[0]==200
    def read():
     for _ in range(40):
      answer,_=query(base,'stable.test');a=answer[0];assert a['displayName'].split()[-1]==a['labels'][0]['val'][1:]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
     futures=[pool.submit(update),pool.submit(read),pool.submit(read)];[f.result() for f in futures]
    checks.append('concurrent_generation_consistent_profile_hydration')
   finally:stop(child)
  for label,kwargs in [('missing',{'omit_footer':True}),('wrong',{'bad_footer':True})]:
   path=root/(label+'.jsonl');write_seed(path,rows[:2],**kwargs)
   bad=subprocess.run([str(BIN),'import','--data',str(root/label),'--input',str(path),'--allow-partial','true'],capture_output=True,text=True,timeout=20);assert bad.returncode!=0
   status=subprocess.run([str(BIN),'status','--data',str(root/label)],capture_output=True,text=True,timeout=10);assert not json.loads(status.stdout)['import_complete']
  checks.append('partial_or_corrupt_import_never_ready')
  result={'passed':True,'checks':checks,'actor_count':len(rows),'cloud_resources':False,'payload_removed':True}
 Path(os.environ.get('TA_SMOKE_RECEIPT',str(ROOT/'smoke-results.json'))).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()

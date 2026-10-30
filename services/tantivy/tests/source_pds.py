import copy,hashlib,json,os,secrets,subprocess,tempfile
from pathlib import Path
import smoke
ROOT=Path(__file__).resolve().parents[1]
BIN=Path(os.environ.get('TA_BINARY',str(ROOT/'target/release/typeahead-tantivy')))
smoke.BIN=BIN

def status(base):
 code,_,value=smoke.request(base,'/index/status');assert code==200
 return value

def counts(base,missing,invalid,hostname,long):
 value=status(base)
 assert value['pds_missing_actor_count']==missing,value
 assert value['pds_invalid_actor_count']==invalid,value
 assert value['pds_unknown_actor_count']==missing+invalid,value
 reasons=value['pds_invalid_reason_counts']
 assert reasons.get('invalid_hostname',0)==hostname,reasons
 assert reasons.get('too_long',0)==long,reasons
 assert set(reasons)<= {'invalid_hostname','too_long'},reasons
 return value

def apply(admin,secret,actors,**extra):
 code,_,value=smoke.request(admin,'/admin/apply',{'actors':actors,**extra},secret)
 return code,value

def main():
 checks=[]
 with tempfile.TemporaryDirectory(prefix='tantivy-source-pds-') as temporary:
  root=Path(temporary);data=root/'data';seed=root/'seed.jsonl';secret=secrets.token_urlsafe(32)
  rows=[smoke.actor(1,'bad-source.test','Scope Pair',100,pds='https://local.test/path'),
        smoke.actor(2,'valid-source.test','Scope Pair',1,pds='HTTPS://LOCAL.TEST./'),
        smoke.actor(3,'missing-source.test','Scope Pair',10,pds=''),
        smoke.actor(4,'long-source.test','Scope Pair',50,pds='x'*9000),
        smoke.actor(5,'bad-avatar.test','Source Avatar',20,pds='http://localhost:2583'),
        smoke.actor(6,'explicit-avatar.test','Explicit Avatar',20,pds='local.test:443'),
        smoke.actor(7,'hidden-source.test','Hidden Source',20,pds='bad host',hidden=True)]
  rows[0]['pds_host']='local.test'
  rows[5]['avatar_url']='https://images.test/path/avatar@jpeg'
  smoke.write_seed(seed,rows)
  imported=subprocess.run([str(BIN),'import','--data',str(data),'--input',str(seed),'--allow-partial','true','--batch-size','2'],capture_output=True,text=True,timeout=30)
  assert imported.returncode==0,imported.stderr
  metadata=json.loads(imported.stdout)
  assert metadata['actor_count']==len(rows) and metadata['input_actors']==len(rows)
  assert metadata['pds_invalid_actor_count']==5 and metadata['pds_missing_actor_count']==1
  checks.append('malformed_optional_pds_does_not_skip_any_actor')
  with (root/'server.log').open('w') as log:
   child,base,admin=smoke.start(data,log,secret)
   try:
    counts(base,1,5,4,1)
    answer,_=smoke.query(base,'Scope Pair')
    assert [a['did'] for a in answer]==[rows[0]['did'],rows[3]['did'],rows[2]['did'],rows[1]['did']]
    scoped,headers=smoke.query(base,'Scope Pair',pds='local.test')
    assert [a['did'] for a in scoped]==[rows[1]['did']] and headers['x-typeahead-pds']=='local.test'
    preferred,_=smoke.query(base,'Scope Pair',prefer_pds='local.test',preference='pds_first')
    assert preferred[0]['did']==rows[1]['did']
    boosted,_=smoke.query(base,'Scope Pair',prefer_pds='local.test',boost=1000)
    assert boosted[0]['did']==rows[1]['did']
    checks.append('invalid_source_never_matches_scope_or_preference_even_with_spoofed_host')
    bad_avatar,_=smoke.query(base,'bad-avatar.test')
    assert bad_avatar[0]['avatar'].startswith('https://cdn.bsky.app/') and 'avatarFallback' not in bad_avatar[0]
    explicit,_=smoke.query(base,'explicit-avatar.test')
    assert explicit[0]['avatarFallback']==rows[5]['avatar_url']
    checks.append('unscopable_localhost_port_url_cannot_form_derived_avatar_url_explicit_avatar_preserved')
    for value in ['https://local.test/path','local.test:443','http://user@local.test','bad host','x'*9000]:
     for param in ['pds','prefer_pds']:
      query='/search?'+smoke.urllib.parse.urlencode({'q':'Scope Pair',param:value})
      code,_,_=smoke.request(base,query);assert code==400,(param,len(value),code)
    checks.append('invalid_query_scopes_and_preferences_remain_rejected')
    changed=copy.deepcopy(rows[0]);changed.update(version=101,pds='https://local.test')
    assert apply(admin,secret,[changed])[0]==200;counts(base,1,4,3,1)
    scoped,_=smoke.query(base,'Scope Pair',pds='local.test');assert scoped[0]['did']==rows[0]['did']
    changed.update(version=102,pds='https://local.test/path')
    assert apply(admin,secret,[changed])[0]==200;counts(base,1,5,4,1)
    changed.update(version=103,pds='')
    assert apply(admin,secret,[changed])[0]==200;counts(base,2,4,3,1)
    changed.update(version=104,pds='local.test:443')
    assert apply(admin,secret,[changed])[0]==200;counts(base,1,5,4,1)
    tombstone=copy.deepcopy(changed);tombstone.update(version=105,deleted=True)
    assert apply(admin,secret,[tombstone])[0]==200;counts(base,1,4,3,1)
    assert apply(admin,secret,[changed])[1]['stale']==1;counts(base,1,4,3,1)
    checks.append('invalid_missing_valid_delete_counters_transition_atomically')
    before=counts(base,1,4,3,1)
    invalid=copy.deepcopy(rows[5]);invalid.update(version=101,display_name='x'*257)
    repaired=copy.deepcopy(rows[3]);repaired.update(version=101,pds='https://local.test')
    code,_=apply(admin,secret,[repaired,invalid],source_cursor={'invalid':'batch'},expected_source_cursor=None)
    assert code==400,code
    after=counts(base,1,4,3,1);assert after['source_cursor']==before['source_cursor']
    for too_big in [1e100,-1e100]:
     invalid=copy.deepcopy(rows[5]);invalid.update(version=101,quality_score=too_big)
     assert apply(admin,secret,[invalid])[0] in (400,422)
    counts(base,1,4,3,1)
    checks.append('relaxed_optional_metadata_does_not_relax_score_or_lexical_admission')
   finally:smoke.stop(child)
   child,base,admin=smoke.start(data,log,secret)
   try:
    counts(base,1,4,3,1)
    assert apply(admin,secret,[rows[3]])[1]['replayed']==1
    assert apply(admin,secret,[rows[5]])[1]['replayed']==1
    assert apply(admin,secret,[tombstone])[1]['replayed']==1
    assert not any(a['did']==rows[0]['did'] for a in smoke.query(base,'Scope Pair')[0])
    checks.append('raw_invalid_pds_and_long_value_survive_committed_restart_and_replay')
   finally:smoke.stop(child)
  max_host='.'.join(['a'*63,'b'*63,'c'*63,'d'*61])
  maximum=smoke.actor(8,'maximum-host.test','Maximum Host',1,pds='HTTPS://'+max_host.upper()+'./')
  assert len(maximum['pds'])==263
  path=root/'maximum.jsonl';smoke.write_seed(path,[maximum])
  result=subprocess.run([str(BIN),'import','--data',str(root/'maximum'),'--input',str(path),'--allow-partial','true'],capture_output=True,text=True,timeout=15)
  assert result.returncode==0,result.stderr
  assert json.loads(result.stdout)['pds_invalid_actor_count']==0
  checks.append('longest_valid_native_host_and_wrapper_are_not_misclassified')
  receipt={'passed':True,'checks':checks,'actors_retained':len(rows),'source_invalid_initial':5,'source_missing_initial':1,'fixed_reason_categories':['invalid_hostname','too_long'],'cloud_resources':False,'payload_removed':True,'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest()}
 destination=Path(os.environ.get('TA_SOURCE_PDS_RECEIPT',str(ROOT/'source-pds-results.json')))
 destination.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()

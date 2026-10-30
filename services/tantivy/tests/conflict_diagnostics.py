import copy,hashlib,json,os,secrets,subprocess,tempfile
from pathlib import Path
import smoke
ROOT=Path(__file__).resolve().parents[1]
BIN=Path(os.environ.get('TA_BINARY',str(ROOT/'target/release/typeahead-tantivy')))
smoke.BIN=BIN

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def apply(admin,secret,actors,cursor,expected):
 return smoke.request(admin,'/admin/apply',{'actors':actors,'source_cursor':cursor,'expected_source_cursor':expected},secret)

def main():
 checks=[]
 with tempfile.TemporaryDirectory(prefix='tantivy-conflict-diagnostics-') as temporary:
  root=Path(temporary);data=root/'data';seed=root/'seed.jsonl';log_path=root/'server.log';secret=secrets.token_urlsafe(32)
  rows=[smoke.actor(1,'private-actor.test','Initial Actor'),smoke.actor(2,'second-actor.test','Initial Second')]
  smoke.write_seed(seed,rows)
  imported=subprocess.run([str(BIN),'import','--data',str(data),'--input',str(seed),'--allow-partial','true'],capture_output=True,text=True,timeout=20)
  assert imported.returncode==0,imported.stderr
  marker='never-log-private-cursor-marker'
  cursors=[{'page':i,'private_marker':marker,'actors':{'did':rows[0]['did']}} for i in range(6)]
  wrong={'private_marker':marker,'actors':{'did':rows[1]['did']},'page':-1}
  current=copy.deepcopy(rows[0]);current.update(version=101,display_name='Never Log Profile Display',avatar_url='https://images.test/never-log-avatar',labels=[{'val':'never-log-label'}],associated={'hidden_marker':'never-log-associated'})
  with log_path.open('w') as log:
   child,base,admin=smoke.start(data,log,secret)
   try:
    code,_,receipt=apply(admin,secret,[current],cursors[1],None);assert code==200 and receipt['applied']==1
    code,_,error=apply(admin,secret,[current],cursors[2],wrong)
    assert code==409 and error=={'error':'SourceVersionConflict'}
    assert smoke.request(base,'/index/status')[2]['source_cursor']==cursors[1]
    code,_,receipt=apply(admin,secret,[current],cursors[2],cursors[1])
    assert code==200 and receipt['replayed']==1 and receipt['applied']==0
    checks.append('cursor_cas_failure_retains_state_and_identical_retry_with_current_cursor_succeeds')
    conflicting=copy.deepcopy(current);conflicting.update(display_name='Never Log Conflicting Display',avatar_url='https://images.test/never-log-conflicting-avatar',labels=[{'val':'never-log-conflicting-label'}],associated={'hidden_marker':'never-log-conflicting-associated'},deleted=True)
    second=copy.deepcopy(rows[1]);second.update(version=101,display_name='Never Commit This Batch')
    code,_,error=apply(admin,secret,[second,conflicting],cursors[3],cursors[2])
    assert code==409 and error=={'error':'SourceVersionConflict'}
    assert smoke.request(base,'/index/status')[2]['source_cursor']==cursors[2]
    assert smoke.query(base,'second-actor.test')[0][0]['displayName']==rows[1]['display_name']
    assert smoke.query(base,'private-actor.test')[0][0]['displayName']==current['display_name']
    code,_,receipt=apply(admin,secret,[current],cursors[3],cursors[2])
    assert code==200 and receipt['replayed']==1
    checks.append('equal_version_conflict_does_not_commit_any_actor_or_cursor_and_valid_replay_still_succeeds')
    tombstone=copy.deepcopy(current);tombstone.update(version=102,deleted=True)
    code,_,receipt=apply(admin,secret,[tombstone],cursors[4],cursors[3]);assert code==200
    resurrected=copy.deepcopy(tombstone);resurrected['deleted']=False
    assert apply(admin,secret,[resurrected],cursors[5],cursors[4])[0]==409
    assert not smoke.query(base,'private-actor.test')[0]
    code,_,receipt=apply(admin,secret,[tombstone],cursors[5],cursors[4]);assert code==200 and receipt['replayed']==1
    checks.append('same_version_live_deleted_ambiguity_is_rejected_in_both_directions')
   finally:smoke.stop(child)
   child,base,admin=smoke.start(data,log,secret)
   try:
    assert smoke.request(base,'/index/status')[2]['source_cursor']==cursors[5]
    code,_,receipt=apply(admin,secret,[tombstone],cursors[5],cursors[5]);assert code==200 and receipt['replayed']==1
    assert not smoke.query(base,'private-actor.test')[0]
    checks.append('committed_cursor_tombstone_and_identical_replay_survive_restart')
   finally:smoke.stop(child)
  log_text=log_path.read_text()
  events=[json.loads(line) for line in log_text.splitlines() if line.startswith('{')]
  conflicts=[e for e in events if e.get('event') in ['source_cursor_conflict','actor_equal_version_conflict']]
  assert len(conflicts)==3,conflicts
  cas=next(e for e in conflicts if e['event']=='source_cursor_conflict')
  assert set(cas)=={'event','expected_cursor_sha256','committed_cursor_sha256','incoming_cursor_sha256'}
  assert cas['expected_cursor_sha256']==digest(wrong)
  assert cas['committed_cursor_sha256']==digest(cursors[1])
  assert cas['incoming_cursor_sha256']==digest(cursors[2])
  actors=[e for e in conflicts if e['event']=='actor_equal_version_conflict']
  allowed={'event','did_sha256','stored_version','incoming_version','old_deleted','incoming_deleted','differing_fields'}
  for e in actors:
   assert set(e)==allowed and e['did_sha256']==hashlib.sha256(rows[0]['did'].encode()).hexdigest()
  assert actors[0]['stored_version']==actors[0]['incoming_version']==101
  assert actors[0]['old_deleted'] is False and actors[0]['incoming_deleted'] is True
  assert set(actors[0]['differing_fields'])=={'display_name','avatar_url','labels','associated','deleted'}
  assert actors[1]['stored_version']==actors[1]['incoming_version']==102
  assert actors[1]['old_deleted'] is True and actors[1]['incoming_deleted'] is False
  assert actors[1]['differing_fields']==['deleted']
  checks.append('fixed_diagnostic_events_distinguish_cursor_cas_and_actor_version_conflicts')
  forbidden=[secret,marker,*[a['did'] for a in rows],current['display_name'],current['avatar_url'],'never-log-label','never-log-associated',conflicting['display_name'],conflicting['avatar_url'],'never-log-conflicting-label','never-log-conflicting-associated']
  assert all(value not in log_text for value in forbidden)
  assert all(len(json.dumps(e,separators=(',',':')))<1024 for e in conflicts)
  checks.append('stderr_contains_no_raw_did_profile_cursor_or_secret_and_events_are_bounded')
  receipt={'passed':True,'checks':checks,'cursor_conflict_events':1,'actor_conflict_events':2,'maximum_diagnostic_bytes':max(len(json.dumps(e,separators=(',',':'))) for e in conflicts),'public_409_body_unchanged':True,'conflict_policy_unchanged':True,'payload_removed':True,'cloud_resources':False,'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest()}
 destination=Path(os.environ.get('TA_CONFLICT_DIAGNOSTICS_RECEIPT',str(ROOT/'conflict-diagnostics-results.json')))
 destination.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()

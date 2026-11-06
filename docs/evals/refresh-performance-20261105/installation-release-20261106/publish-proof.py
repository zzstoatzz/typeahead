import hashlib,json,shlex,subprocess,time,urllib.request
from pathlib import Path
import boto3
from prefect.blocks.system import Secret
from botocore.exceptions import ClientError
root=Path('/tmp/typeahead-fast-install-release');repo=Path('/Users/nate/tangled.org/zzstoatzz.io/typeahead')
def read(p):return json.loads(p.read_bytes())
def status(origin):
 with urllib.request.urlopen(urllib.request.Request(origin+'/index/status',headers={'User-Agent':'typeahead-install-release/1'}),timeout=15) as r:raw=r.read(1048577)
 assert len(raw)<=1048576;return json.loads(raw)
plan=read(root/'plan.json');journal=read(root/'journal.json');image=read(root/'image.json')['image'];expected=read(root/'expected-image-files.json')
assert journal['phase']=='prepared' and plan['image_digest']==image and plan['test_only'] is False
assert hashlib.sha256((root/'plan.json').read_bytes()).hexdigest()==journal['plan_sha256']
matched=read(root/'matched-performance.json');assert matched['passed'] and len(matched['pairs'])==39 and matched['machine_id']==plan['machine_id']
fixture=read(repo/'docs/evals/refresh-performance-20261105/heavypad-correctness.json');assert fixture['returncode']==0 and fixture['test_count']==8
assert fixture['test_source_sha256']==hashlib.sha256((repo/'scripts/snapshot_refresh/test_tantivy_slot.py').read_bytes()).hexdigest()
for n in ['tantivy-slot.py','tantivy_install.py']:assert expected['/app/'+n]==hashlib.sha256((repo/'scripts/snapshot_refresh'/n).read_bytes()).hexdigest()
machines=json.loads(subprocess.check_output(['fly','machine','list','-a',plan['app'],'--json']))
m=next(x for x in machines if x['id']==plan['machine_id']);cfg=m['config'];assert m['state']=='started' and cfg['image']==image and cfg['guest']==dict(cpu_kind='shared',cpus=2,memory_mb=1024)
assert len(cfg['mounts'])==1 and cfg['mounts'][0]['volume']==plan['volume_id'] and cfg['mounts'][0]['path']=='/data' and cfg['mounts'][0]['size_gb']==20 and cfg['mounts'][0]['encrypted'] is True
program='import hashlib,json,pathlib;names='+repr(list(expected))+';print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in names}))'
result=json.loads(subprocess.check_output(['fly','machine','exec',plan['machine_id'],'python3 -c '+shlex.quote(program),'-a',plan['app'],'--json','--timeout','20']))
assert result.get('exit_code',0)==0;actual=json.loads(result['stdout']);assert actual==expected
(root/'actual-image-files.json').write_text(json.dumps(actual,indent=2)+'\n')
live=status('https://'+plan['app']+'.fly.dev');assert live['ready'] and live['machine_id']==plan['machine_id'] and live['instance_id']==m['instance_id'] and live['build_id']==plan['candidate']['build_id'] and live['binary_sha256']==expected['/app/typeahead-tantivy']
assert live['full_source'] and live['import_complete'] and live['expiry_coverage_ready'] and live['moderation']['fresh'] and live['pds_scope_version']==1 and live['actor_count']>13000000
assert live['query_config']['max_sync_lag_s']==120 and live['query_config']['allow_stale'] is False and live['sync_lag_s']<=120
events=read(root/'machine-events.json');starts=[e['timestamp'] for e in events if e['type']=='start' and e['status']=='started'];stops=[e['timestamp'] for e in events if e['type']=='stop'];assert len(starts)>=2 and min(starts)<max(stops)<max(starts)
assert any(e['type']=='exit' and e.get('request',{}).get('exit_event',{}).get('requested_stop') is True for e in events)
(root/'candidate-status.json').write_text(json.dumps(live,indent=2)+'\n')
worker=read(root/'worker-current.json');assert worker['versions']==[{'version_id':'cc19d59f-486c-4d1d-abcc-faf8e450b705','percentage':100}]
old=read(repo/'docs/ops/lifecycle-cleanup-20261105/release-proof.json');oldfiles=read(repo/'docs/ops/lifecycle-cleanup-20261105/image-files.json')
assert all(actual[p]==v for p,v in oldfiles.items() if p not in ['/app/tantivy-slot.py','/app/tantivy_install.py'])
with urllib.request.urlopen(urllib.request.Request('https://typeahead.waow.tech/health/freshness',headers={'User-Agent':'typeahead-install-release/1'}),timeout=15) as r:health=json.load(r)
assert health['ok'] and health['probe_ok'] and not health['stale'] and health['build_id']==plan['candidate']['build_id']
def secret(n):return Secret.load(n).get()
s3=boto3.client('s3',endpoint_url=secret('index-r2-endpoint'),region_name='auto',aws_access_key_id=secret('index-r2-access-key-id'),aws_secret_access_key=secret('index-r2-secret-access-key'));bucket=secret('index-r2-bucket')
r=s3.get_object(Bucket=bucket,Key='control/search-routing.json')
with r['Body'] as f:route=json.load(f)
assert route==plan['expected_route'] and r['ETag']==plan['expected_current_etag']
binding=dict(binary_sha256=live['binary_sha256'],retrieval_strategy='global-merge',canonical_rules_source_sha256=live['expiry_coverage']['canonical_rules_source_sha256'],image_digest=image,guest=cfg['guest'],volume_size_gib=20)
gates={
'matched_performance':dict(passed=True,result=matched),
'full_functional':dict(passed=True,new_installation_checks=fixture,actual_image_files=actual,unchanged_reader_source_functional=old['gates']['full_functional'],scope='The reader and live-update modules retain their qualified exact hashes. Eight new real-reader/rclone checks cover the changed downloader and installer; full-data matching is measured separately.'),
'live_source_coverage':dict(passed=True,at=time.time(),status=live),
'worker_http_smoke':dict(passed=True,current_public_freshness=health,current_worker_deployment=worker,unchanged_worker_smoke=old['gates']['worker_http_smoke'],scope='Worker unchanged; full-data candidate matching passed. Public aliases and scope must be checked again against the promoted machine.'),
'recovery':dict(passed=True,installation_checks=fixture,managed_cached_restart=journal,provider_restart_events=events,unchanged_controller_recovery=old['gates']['recovery'],scope='Controller unchanged; candidate passed actual managed first start and cached restart. Interruption/corruption recovery covered by bounded real-rclone/reader tests.'),
'refresh_lifecycle':dict(passed=True,plan=plan,journal=journal,scope='Managed inactive-slot installation and cached restart of existing immutable production snapshot. New cold-install timing will be recorded; no duplicate corpus build was requested.')}
def publish(prefix,value):
 raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()+b'\n';sha=hashlib.sha256(raw).hexdigest();key=prefix+sha+'.json'
 try:s3.put_object(Bucket=bucket,Key=key,Body=raw,ContentType='application/json',IfNoneMatch='*')
 except ClientError as e:
  if e.response['ResponseMetadata']['HTTPStatusCode']!=412:raise
 r=s3.get_object(Bucket=bucket,Key=key)
 with r['Body'] as f:assert f.read(len(raw)+1)==raw
 (root/(sha+'.json')).write_bytes(raw)
 return dict(key=key,sha256=sha,verified=True)
published={}
for name,v in gates.items():
 v.update(schema_version=1,binding=binding);p=publish('control/tantivy-release-evidence/',v);published[name]=dict(passed=True,evidence_key=p['key'],evidence_sha256=p['sha256'])
proof=dict(schema_version=1,engine='tantivy',test_only=False,passed=True,binding=binding,gates=published,limitations=['Small paced warm-engine comparison is not a production percentile or sustained-load SLO.','Only the downloader and installer changed; exact-hash reader, live-source, Worker and controller evidence is explicitly carried forward.','A normal scheduled refresh and post-promotion public checks remain separate observations.'])
p=publish('control/tantivy-releases/',proof);(root/'release-proof.json').write_bytes((root/(p['sha256']+'.json')).read_bytes());(root/'release-publication.json').write_text(json.dumps(p,indent=2)+'\n');print(json.dumps(p))

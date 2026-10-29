import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('tantivy_install', HERE / 'tantivy_install.py')
adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
sys.path.insert(0, str(ROOT / 'services/tantivy/tests'))
import smoke


def start_strict(binary,data,log,secret,fixture_identity=False):
    public,admin=smoke.port(),smoke.port()
    environment=dict(os.environ,ADMIN_SECRET=secret)
    if fixture_identity:environment.update(FLY_MACHINE_ID='abcdef12345678',FLY_MACHINE_VERSION='fixture-instance')
    child=subprocess.Popen([str(binary),'serve','--data',str(data),'--listen',f'127.0.0.1:{public}','--admin-listen',f'127.0.0.1:{admin}'],env=environment,stdout=log,stderr=log)
    base=f'http://127.0.0.1:{public}';private=f'http://127.0.0.1:{admin}'
    for _ in range(100):
        if child.poll() is not None:raise RuntimeError('Strict server exited')
        try:
            if smoke.request(base,'/health')[0]==200:return child,base,private
        except OSError:pass
        time.sleep(.02)
    smoke.stop(child);raise RuntimeError('Strict server failed to start')


class AdmissionTests(unittest.TestCase):
    def manifest(self):
        return {'engine':'tantivy','manifest_version':1,'artifact_format':'tantivy-directory-tar-v1','backend_format':adapter.FORMAT,'active_build':'b1793236000-pilot','normalize_version':'v2-cjk-bigram','plan_version':'complete-prefix-groups-v1','source_policy':adapter.SOURCE_POLICY,'sha256':'a'*64,'binary_sha256':'b'*64,'canonical_rules_source_sha256':json.loads((ROOT/'src/moderation-regexes.json').read_text())['canonical_source_sha256'],'retrieval_strategy':'per-segment','image_digest':'registry.fly.io/tantivy-fixture@sha256:'+'e'*64,'guest':dict(adapter.GUEST),'volume_id':'vol_fixture','volume_size_gib':20,'public_service_ports':[],'provider_app':'tantivy-fixture','byte_size':10240,'source_actor_count':1,'object_key':'releases/search-tantivy/b1793236000-pilot/index.tar','files':[{'path':'index/meta.json','byte_size':1,'sha256':'a'*64}]}

    def status(self):
        now=1000000;m=self.manifest()
        cursor={'schema_version':1,'build_id':m['active_build'],'snapshot_watermark':100,'source_policy':adapter.SOURCE_POLICY,'through':now-5,'cycle':None,'reconciliation':{'schema_version':1,'last_completed_at':now-10,'last_completed_started_at':now-100}}
        return {'engine':'tantivy','format':adapter.FORMAT,'build_id':m['active_build'],'normalize_version':m['normalize_version'],'plan_version':m['plan_version'],'watermark':now-5,'snapshot_watermark':100,'source_observation_revision':3,'source_cursor':cursor,'import_complete':True,'full_source':True,'source_manifest':{'partial':False,'actor_count':1,'source_actor_count':1},'input_actors':1,'actor_count':1,'ready':True,'cutover_source_ready':True,'expiry_coverage':{'schema_version':1,'import_complete':True,'full_source':True,'canonical_rules_source_sha256':m['canonical_rules_source_sha256'],'expected_input_actors':1,'source_actor_count':1,'input_actors_checked':1,'verified_input_actors_sha256':'f'*64,'classifier_sha256':'d'*64,'current':{'live_actors':1,'actors_with_unsupported_expiry':0,'actors_with_malformed_labels':0}},'pds_scope_version':1,'binary_sha256':m['binary_sha256'],'retrieval_strategy':'per-segment','app':'tantivy-fixture','machine_id':'abcdef12345678','instance_id':'fixture-instance','moderation':{'enabled':True,'complete':True,'fresh':True,'sha256':'c'*64,'source_observed_at':now-5,'canonical_rules_source_sha256':m['canonical_rules_source_sha256']}}

    def provider(self,now=1000000):
        manifest=self.manifest()
        return {'schema':1,'provider':'fly','observed_at':datetime.fromtimestamp(now,timezone.utc).isoformat().replace('+00:00','Z'),'app':manifest['provider_app'],'machine_id':'abcdef12345678','instance_id':'fixture-instance','state':'started','image_digest':manifest['image_digest'],'guest':manifest['guest'],'mounts':[{'volume_id':manifest['volume_id'],'path':'/data','size_gib':20,'encrypted':True}],'owner':'tantivy-qualification-20261029','role':'tantivy-control','public_service_ports':[],'provider_config_reserved_env_overrides':False,'test_only':True}

    def test_independent_runtime_gates_reject_claimed_readiness_with_missing_evidence(self):
        manifest=self.manifest();status=self.status();adapter.validate_manifest(manifest,manifest['active_build']);adapter.validate_runtime(status,manifest,1000000)
        mutations=[('format','tantivy-0.27-f32-did-source-v2'),('full_source',False),('input_actors',2),('source_observation_revision',0),('ready',False),('cutover_source_ready',False),('watermark',1),('binary_sha256','0'*64),('retrieval_strategy','global-merge')]
        for key,value in mutations:
            with self.subTest(key=key),self.assertRaises(ValueError):adapter.validate_runtime(dict(status,**{key:value}),manifest,1000000)
        for key,value in [('source_policy','turso-current-state-cutoff-v1'),('cycle',{'stream':'actors'}),('through',1)]:
            bad=copy.deepcopy(status);bad['source_cursor'][key]=value
            with self.subTest(cursor=key),self.assertRaises(ValueError):adapter.validate_runtime(bad,manifest,1000000)
        for key,value in [('last_completed_at',None),('last_completed_started_at',None),('last_completed_at',900000),('last_completed_started_at',800000)]:
            bad=copy.deepcopy(status);bad['source_cursor']['reconciliation'][key]=value
            with self.subTest(sweep=key),self.assertRaises(ValueError):adapter.validate_runtime(bad,manifest,1000000)
        for key,value in [('fresh',False),('source_observed_at',999699),('source_observed_at',1000001),('canonical_rules_source_sha256','d'*64)]:
            bad=copy.deepcopy(status);bad['moderation'][key]=value
            with self.subTest(policy=key),self.assertRaises(ValueError):adapter.validate_runtime(bad,manifest,1000000)
        with self.assertRaises(ValueError):adapter.validate_runtime(status,manifest,1000000,max_poll_age=121)
        for field,value in [('input_actors_checked',0),('source_actor_count',2),('verified_input_actors_sha256',None),('canonical_rules_source_sha256','0'*64)]:
            bad=copy.deepcopy(status);bad['expiry_coverage'][field]=value
            with self.subTest(expiry=field),self.assertRaises(ValueError):adapter.validate_runtime(bad,manifest,1000000)
        for field in ['actors_with_unsupported_expiry','actors_with_malformed_labels']:
            bad=copy.deepcopy(status);bad['expiry_coverage']['current'][field]=1
            with self.subTest(expiry=field),self.assertRaises(ValueError):adapter.validate_runtime(bad,manifest,1000000)

    def test_provider_records_require_fresh_independent_final_shape_and_runtime_instance(self):
        manifest=self.manifest();status=self.status();provider=self.provider()
        receipt=adapter.validate_provider(provider,manifest,status,1000000)
        self.assertTrue(receipt['provider_runtime_instance_bound']);self.assertFalse(receipt['provider_resources_qualified']);self.assertTrue(receipt['provider_attestation_test_only'])
        real_claim=dict(provider,test_only=False)
        self.assertTrue(adapter.validate_provider(real_claim,manifest,status,1000000)['provider_resources_qualified'])
        dev_manifest=dict(manifest,public_service_ports=[80,443]);dev_provider=dict(provider,role='dev-tantivy',owner='tantivy-dev-20261028',public_service_ports=[80,443])
        self.assertFalse(adapter.validate_provider(dev_provider,dev_manifest,status,1000000)['provider_resources_qualified'])
        with self.assertRaises(ValueError):adapter.validate_provider(dict(provider,public_service_ports=[80,443]),dev_manifest,status,1000000)
        for key,value in [('provider','self-report'),('schema',2),('observed_at',datetime.fromtimestamp(999879,timezone.utc).isoformat()),('state','stopped'),('instance_id','other'),('image_digest','registry.fly.io/other@sha256:'+'e'*64),('guest',dict(adapter.GUEST,memory_mb=2048)),('role','native-control'),('owner','unowned'),('provider_config_reserved_env_overrides',True),('provider_config_reserved_env_overrides',None),('public_service_ports',[443]),('app','other-app')]:
            bad=copy.deepcopy(real_claim);bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):adapter.validate_provider(bad,manifest,status,1000000)
        for protected in adapter.PROTECTED_MACHINES:
            with self.subTest(protected=protected),self.assertRaises(ValueError):adapter.validate_provider(dict(real_claim,machine_id=protected),manifest,status,1000000)
        for key,value in [('size_gib',40),('volume_id','vol_other'),('encrypted',False),('path','/other')]:
            bad=copy.deepcopy(real_claim);bad['mounts'][0][key]=value
            with self.subTest(mount=key),self.assertRaises(ValueError):adapter.validate_provider(bad,manifest,status,1000000)
        for missing in ['machine_id','instance_id']:
            bad=dict(status);bad.pop(missing)
            with self.subTest(runtime=missing),self.assertRaises(ValueError):adapter.validate_provider(real_claim,manifest,bad,1000000)

    def test_archive_inventory_and_paths_cannot_escape_or_masquerade_as_sqlite(self):
        manifest=self.manifest()
        for path in ['../index/meta.json','index/../evil','/index/meta.json','index/index.db/evil','index/.','index/..','index/writer.lock']:
            bad=copy.deepcopy(manifest);bad['files'][0]['path']=path
            with self.subTest(path=path),self.assertRaises(ValueError):adapter.validate_manifest(bad,bad['active_build'])
        for key,value in [('engine','native'),('artifact_format','FLATPX05'),('backend_format','tantivy-0.27-f32-did-source-v2'),('object_key','releases/search/b1793236000-pilot/index.db')]:
            with self.subTest(key=key),self.assertRaises(ValueError):adapter.validate_manifest(dict(manifest,**{key:value}),manifest['active_build'])

    def test_actual_tar_rejects_traversal_links_and_extra_files_before_reader_execution(self):
        with tempfile.TemporaryDirectory(prefix='tantivy-tar-boundary-') as temporary:
            root=Path(temporary);binary=Path(sys.executable).resolve();outside=root/'outside';outside.write_text('untouched')
            for kind in ['traversal','symlink','hardlink','extra','duplicate']:
                archive=root/(kind+'.tar');manifest=self.manifest()
                with tarfile.open(archive,'w') as tar:
                    item=tarfile.TarInfo('../outside' if kind=='traversal' else 'index/extra' if kind=='extra' else 'index/meta.json')
                    item.size=1
                    if kind in ['symlink','hardlink']:
                        item.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE;item.linkname=str(outside);item.size=0
                        tar.addfile(item)
                    else:
                        tar.addfile(item,io.BytesIO(b'x'))
                        if kind=='duplicate':tar.addfile(item,io.BytesIO(b'x'))
                manifest.update(byte_size=archive.stat().st_size,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
                manifest['files'][0]['sha256']=hashlib.sha256(b'x').hexdigest()
                with self.subTest(kind=kind),self.assertRaises(ValueError):adapter.install_bundle(archive,root/kind,manifest,binary)
                self.assertEqual(outside.read_text(),'untouched');self.assertFalse((root/kind).exists())

    def test_real_binary_directory_install_runtime_qualification_and_corruption(self):
        binary=Path(os.environ.get('TA_BINARY',ROOT/'services/tantivy/target/release/typeahead-tantivy')).resolve()
        if not binary.is_file():self.skipTest('actual Tantivy binary unavailable')
        with tempfile.TemporaryDirectory(prefix='tantivy-install-') as temporary:
            root=Path(temporary);data=root/'data';seed=root/'seed.jsonl';manifest=self.manifest();now=int(time.time());manifest['binary_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
            row=smoke.actor(1,'real.test','Real Actor');lines=json.dumps(row,separators=(',',':')).encode()+b'\n'
            header={'type':'manifest','build_id':manifest['active_build'],'source_watermark':100,'actor_count':1,'source_actor_count':1,'normalize_version':manifest['normalize_version'],'partial':False}
            seed.write_bytes(json.dumps(header).encode()+b'\n'+lines+json.dumps({'type':'end','build_id':header['build_id'],'actor_count':1,'actors_sha256':hashlib.sha256(lines).hexdigest()}).encode()+b'\n')
            result=subprocess.run([str(binary),'import','--data',str(data),'--input',str(seed)],capture_output=True,timeout=30);self.assertEqual(result.returncode,0,result.stderr)
            secret=secrets.token_urlsafe(24)
            with (root/'server.log').open('wb') as log:
                child,public,admin=start_strict(binary,data,log,secret)
                try:
                    state=smoke.request(admin,'/admin/source-state',{'dids':[]},secret)[2]
                    cursor=self.status()['source_cursor'];cursor.update(through=now,actors={'updated_at':now,'did':''},tombstones={'deleted_at':now,'did':''},lookback_seconds=300)
                    cursor['reconciliation'].update(last_completed_at=now-1,last_completed_started_at=now-10)
                    body={'actors':[],'expected_documents':[],'expected_source_cursor':state['source_cursor'],'source_cursor':cursor,'expected_observation_revision':state['source_observation_revision'],'batch_id':hashlib.sha256(b'install-qualified').hexdigest(),'watermark':now,'policy_snapshot':{'schema_version':1,'source_observed_at':now,'complete':True,'overrides':[],'blocked_domains':[]}}
                    code,_,reply=smoke.request(admin,'/admin/source-reconcile',body,secret);self.assertEqual(code,200,reply)
                    status=smoke.request(public,'/index/status')[2]
                    adapter.validate_runtime(status,manifest)
                finally:smoke.stop(child)
            bundle=root/'index.tar';manifest['files']=[]
            with tarfile.open(bundle,'w') as archive:
                for path in sorted((data/'index').iterdir()):
                    if path.is_file() and not path.name.endswith('.lock'):
                        name='index/'+path.name;archive.add(path,arcname=name,recursive=False);manifest['files'].append({'path':name,'byte_size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            manifest.update(byte_size=bundle.stat().st_size,sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
            receipt=adapter.install_bundle(bundle,root/'installed',manifest,binary)
            self.assertFalse(receipt['runtime_qualified']);self.assertTrue((root/'installed/index/meta.json').is_file());self.assertFalse((root/'installed/index.db').exists())
            qualified=adapter.qualify_route(status,manifest,receipt,provider_attestation=self.provider(now));self.assertEqual(qualified['route']['engine'],'tantivy');self.assertFalse(qualified['admission']['slo_empirically_qualified'])
            with self.assertRaises(ValueError):adapter.qualify_route(status,manifest,dict(receipt,manifest_sha256='0'*64),provider_attestation=self.provider(now))
            wrong=copy.deepcopy(manifest);wrong['files'][0]['sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'checksum'):adapter.install_bundle(bundle,root/'corrupt',wrong,binary)
            self.assertFalse((root/'corrupt').exists());self.assertTrue((root/'installed/index/meta.json').exists())
            with self.assertRaisesRegex(ValueError,'already exists'):adapter.install_bundle(bundle,root/'installed',manifest,binary)
            with (root/'restarted.log').open('wb') as log:
                child,public,admin=start_strict(binary,root/'installed',log,secret,fixture_identity=True)
                try:
                    live=adapter.qualify_runtime(public,manifest,receipt,allow_loopback=True,provider_attestation=self.provider(int(time.time())))
                    self.assertEqual(live['route']['admission_sha256'],adapter.digest_json(live['admission']))
                    self.assertTrue(live['admission']['test_only']);self.assertFalse(live['admission']['runtime_http_verified']);self.assertEqual(live['admission']['runtime_observed_origin'],public);self.assertTrue(live['admission']['provider_runtime_instance_bound']);self.assertFalse(live['admission']['provider_resources_qualified'])
                finally:smoke.stop(child)
            staged=adapter.install_bundle(bundle,root/'stale-stage',manifest,binary,now=now+400)
            self.assertFalse(staged['runtime_qualified'])
            with self.assertRaises(ValueError):adapter.qualify_route(status,manifest,staged,now=now+400)


if __name__=='__main__':unittest.main()

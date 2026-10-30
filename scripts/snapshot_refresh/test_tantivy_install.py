import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
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
        return {'engine':'tantivy','manifest_version':1,'artifact_format':'tantivy-directory-tar-v1','backend_format':adapter.FORMAT,'active_build':'b1793236000-pilot','normalize_version':'v2-cjk-bigram','plan_version':'complete-prefix-groups-v1','source_policy':adapter.SOURCE_POLICY,'sha256':'a'*64,'binary_sha256':'b'*64,'canonical_rules_source_sha256':json.loads((ROOT/'src/moderation-regexes.json').read_text())['canonical_source_sha256'],'retrieval_strategy':'per-segment','image_digest':'registry.fly.io/tantivy-fixture@sha256:'+'e'*64,'guest':dict(adapter.GUEST),'volume_id':'vol_fixture','volume_size_gib':20,'public_service_ports':[],'provider_app':'tantivy-fixture','byte_size':10240,'source_actor_count':1,'source_build_id':'b1793236000-pilot','legacy_build_id':'b1793236000-native','source_manifest_sha256':'1'*64,'source_watermark':100,'source_database_sha256':'2'*64,'source_prefix_sha256':'3'*64,'source_actors_sha256':'f'*64,'object_key':'releases/search-tantivy/b1793236000-pilot/index.tar','files':[{'path':'index/meta.json','byte_size':1,'sha256':'a'*64}]}

    def status(self):
        now=1000000;m=self.manifest()
        cursor={'schema_version':1,'build_id':m['active_build'],'snapshot_watermark':100,'source_policy':adapter.SOURCE_POLICY,'through':now-5,'cycle':None,'reconciliation':{'schema_version':1,'last_completed_at':now-10,'last_completed_started_at':now-100}}
        return {'engine':'tantivy','format':adapter.FORMAT,'build_id':m['active_build'],'normalize_version':m['normalize_version'],'plan_version':m['plan_version'],'watermark':now-5,'snapshot_watermark':100,'source_observation_revision':3,'source_cursor':cursor,'import_complete':True,'full_source':True,'source_manifest':{'build_id':m['source_build_id'],'source_watermark':m['source_watermark'],'source_database_sha256':m['source_database_sha256'],'source_prefix_sha256':m['source_prefix_sha256'],'partial':False,'actor_count':1,'source_actor_count':1},'input_actors':1,'actor_count':1,'ready':True,'cutover_source_ready':True,'expiry_coverage':{'schema_version':1,'import_complete':True,'full_source':True,'canonical_rules_source_sha256':m['canonical_rules_source_sha256'],'expected_input_actors':1,'source_actor_count':1,'input_actors_checked':1,'verified_input_actors_sha256':'f'*64,'classifier_sha256':'d'*64,'current':{'live_actors':1,'actors_with_unsupported_expiry':0,'actors_with_malformed_labels':0}},'pds_scope_version':1,'binary_sha256':m['binary_sha256'],'retrieval_strategy':'per-segment','app':'tantivy-fixture','machine_id':'abcdef12345678','instance_id':'fixture-instance','moderation':{'enabled':True,'complete':True,'fresh':True,'sha256':'c'*64,'source_observed_at':now-5,'canonical_rules_source_sha256':m['canonical_rules_source_sha256']}}

    def provider(self,now=1000000):
        manifest=self.manifest()
        return {'schema':1,'provider':'fly','observed_at':datetime.fromtimestamp(now,timezone.utc).isoformat().replace('+00:00','Z'),'app':manifest['provider_app'],'machine_id':'abcdef12345678','instance_id':'fixture-instance','state':'started','image_digest':manifest['image_digest'],'guest':manifest['guest'],'mounts':[{'volume_id':manifest['volume_id'],'path':'/data','size_gib':20,'encrypted':True}],'owner':'tantivy-qualification-20261029','role':'tantivy-control','public_service_ports':[],'provider_config_reserved_env_overrides':False,'test_only':True}

    def checkpoint_manifest(self):
        manifest = self.manifest()
        native = {'manifest_version':2,'format_version':5,'active_build':'b1792000000-rollback-px05',
                  'actor_count':7,'source_watermark':50,'sha256':'2'*64,
                  'normalize_version':manifest['normalize_version'],'prefix_map':{'sha256':'3'*64},
                  'legacy_twin':{'build_id':'b1792000000-rollback','source_watermark':50}}
        native_raw = json.dumps(native,sort_keys=True,separators=(',',':')).encode()+b'\n'
        receipt = {'schema_version':1,'kind':adapter.CHECKPOINT_SOURCE,'fixture':True,
                   'generation':7,'generation_after':8,'source_origin_sha256':'4'*64,
                   'source_clock_before':1000000,'source_clock_after':1000002,
                   'base_db_sha256':'5'*64,'base_db_bytes':4096,'received_wal_frames':2,
                   'committed_frame':1,'committed_provider_frames_sha256':'6'*64,
                   'replayed_wal_sha256':'7'*64,'replayed_wal_bytes':4152,
                   'database_sha256':'8'*64,'database_bytes':8192,
                   'sqlite_replay_verified':True,'sqlite_verified_wal_frames':1,
                   'boundary_kind':'successful-short-WAL-batch','received_source_bytes':12350,
                   'source_requests':6,'wall_seconds':0.1,'live_coverage_verified':False,
                   'policy_freshness_attested':False}
        receipt_raw = json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()+b'\n'
        receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
        anchor = {'build_id':native['active_build'],'manifest_sha256':hashlib.sha256(native_raw).hexdigest(),
                  'database_sha256':native['sha256'],'prefix_sha256':native['prefix_map']['sha256'],
                  'actor_count':native['actor_count'],'source_watermark':native['source_watermark'],
                  'legacy_build_id':native['legacy_twin']['build_id']}
        manifest.update(source_kind=adapter.CHECKPOINT_SOURCE,source_policy=adapter.CHECKPOINT_POLICY,
                        source_build_id='b1793236000-primary',source_manifest_sha256=receipt_sha,
                        source_database_sha256=receipt['database_sha256'],source_actor_count=4,
                        source_watermark=999700,legacy_build_id=anchor['legacy_build_id'],
                        rollback_native_anchor=anchor,
                        source_bootstrap={'schema_version':1,'kind':adapter.CHECKPOINT_SOURCE,
                            'coverage':'checkpoint-only','live_coverage_verified':False,
                            'checkpoint_receipt_sha256':receipt_sha,'checkpoint_receipt':receipt,
                            'replay_floor':999700,'replay_overlap_seconds':300,
                            'actors_sha256':manifest['source_actors_sha256'],
                            'counts':{'raw_actor_rows':3,'orphan_tombstones':1,'input_documents':4,
                                      'live_actor_documents':2,'deleted_actor_documents':2,'native_visible_actors':None}})
        del manifest['source_prefix_sha256']
        return manifest,receipt_raw,native_raw

    def test_checkpoint_primary_counts_and_independent_rollback_are_structural_only(self):
        manifest,receipt_raw,native_raw = self.checkpoint_manifest()
        adapter.validate_artifact_manifest(manifest,manifest['active_build'])
        result = adapter.validate_checkpoint_provenance(manifest,receipt_raw,native_raw)
        self.assertTrue(result['structurally_valid'])
        self.assertFalse(result['live_admission'])
        self.assertEqual(result['coverage'],'checkpoint-only')
        self.assertIsNone(result['counts']['native_visible_actors'])
        self.assertNotEqual(result['counts']['input_documents'],manifest['rollback_native_anchor']['actor_count'])
        self.assertNotEqual(manifest['source_watermark'],manifest['rollback_native_anchor']['source_watermark'])
        self.assertEqual(adapter.validate_source_manifest(manifest,native_raw,checkpoint_receipt_bytes=receipt_raw),result)
        with self.assertRaises(ValueError):adapter.validate_source_manifest(manifest,native_raw)
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(manifest,receipt_raw+b' ',native_raw)
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(manifest,receipt_raw,native_raw+b' ')
        known=copy.deepcopy(manifest)
        known['source_bootstrap']['counts']['native_visible_actors']=1
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(known,receipt_raw,native_raw)
        known['source_bootstrap']['native_visible_database_sha256']=manifest['source_database_sha256']
        self.assertEqual(adapter.validate_checkpoint_provenance(known,receipt_raw,native_raw)['counts']['native_visible_actors'],1)

    def test_checkpoint_provenance_rejects_floor_prefix_wal_count_and_anchor_confusion(self):
        original,receipt_raw,native_raw = self.checkpoint_manifest()
        cases=[(('source_bootstrap','counts','raw_actor_rows'),4),
               (('source_bootstrap','counts','live_actor_documents'),1),
               (('source_bootstrap','counts','deleted_actor_documents'),3),
               (('source_bootstrap','counts','orphan_tombstones'),2),
               (('source_bootstrap','counts','input_documents'),7),
               (('source_bootstrap','counts','native_visible_actors'),7),
               (('source_bootstrap','counts','input_documents'),True),
               (('source_bootstrap','replay_floor'),1000000),
               (('source_bootstrap','replay_overlap_seconds'),0),
               (('source_bootstrap','live_coverage_verified'),True),
               (('source_bootstrap','schema_version'),True),
               (('source_bootstrap','checkpoint_receipt','source_clock_after'),999999),
               (('source_bootstrap','checkpoint_receipt','generation_after'),6),
               (('source_bootstrap','checkpoint_receipt','committed_frame'),3),
               (('source_bootstrap','checkpoint_receipt','sqlite_verified_wal_frames'),0),
               (('source_bootstrap','checkpoint_receipt','sqlite_replay_verified'),False),
               (('source_bootstrap','checkpoint_receipt','replayed_wal_bytes'),4153),
               (('source_bootstrap','checkpoint_receipt','boundary_kind'),'error-EOF'),
               (('source_bootstrap','checkpoint_receipt','policy_freshness_attested'),True),
               (('source_bootstrap','actors_sha256'),'9'*64),
               (('rollback_native_anchor','actor_count'),4),
               (('rollback_native_anchor','source_watermark'),999700),
               (('source_database_sha256',),'2'*64),
               (('source_manifest_sha256',),original['rollback_native_anchor']['manifest_sha256']),
               (('source_policy',),adapter.SOURCE_POLICY),
               (('source_prefix_sha256',),'3'*64)]
        for path,value in cases:
            changed=copy.deepcopy(original);target=changed
            for key in path[:-1]:target=target[key]
            target[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(ValueError):
                adapter.validate_checkpoint_provenance(changed,receipt_raw,native_raw)

    def test_checkpoint_only_cannot_freeze_install_or_claim_runtime_even_with_ready_flags(self):
        manifest,_,_=self.checkpoint_manifest()
        with self.assertRaisesRegex(ValueError,'Checkpoint-only source is not admitted'):
            adapter.freeze_bundle('/missing/data','/missing/destination',manifest,b'','/missing/binary')
        with self.assertRaisesRegex(ValueError,'Checkpoint-only source is not admitted'):
            adapter.install_bundle('/missing/bundle','/missing/destination',manifest,'/missing/binary')
        with self.assertRaisesRegex(ValueError,'Checkpoint-only source is not admitted'):
            adapter.validate_runtime({'ready':True,'cutover_source_ready':True},manifest)
        with self.assertRaisesRegex(ValueError,'Checkpoint-only source is not admitted'):
            adapter.validate_metadata({},manifest,require_source_ready=False,require_coverage_ready=False)

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

    def test_managed_slots_bind_origin_provider_owner_and_installation(self):
        for app in sorted(adapter.SLOT_APPS):
            manifest=dict(self.manifest(),provider_app=app,public_service_ports=[80,443])
            status=dict(self.status(),app=app)
            provider=dict(self.provider(),app=app,role='tantivy-slot',owner='typeahead-snapshot-refresh',public_service_ports=[80,443],test_only=False)
            installation={'passed':True,'manifest_sha256':adapter.digest_json(manifest),'binary_sha256':manifest['binary_sha256'],'bundle_sha256':manifest['sha256'],'build_id':manifest['active_build']}
            result=adapter.qualify_route(status,manifest,installation,now=1000000,provider_attestation=provider)
            self.assertEqual(result['route']['origin'],f'https://{app}.fly.dev')
            self.assertFalse(result['admission']['runtime_http_verified'])
            with self.assertRaises(ValueError):adapter.qualify_route(status,manifest,installation,now=1000000,observed_origin=adapter.ORIGIN,provider_attestation=provider)
            for field,value in [('owner','other'),('app','unapproved'),('public_service_ports',[])]:
                with self.subTest(app=app,field=field),self.assertRaises(ValueError):adapter.validate_provider(dict(provider,**{field:value}),manifest,status,1000000)
        manifest=self.manifest();status=self.status();provider=dict(self.provider(),test_only=False)
        installation={'passed':True,'manifest_sha256':adapter.digest_json(manifest),'binary_sha256':manifest['binary_sha256'],'bundle_sha256':manifest['sha256'],'build_id':manifest['active_build']}
        with self.assertRaisesRegex(ValueError,'Private qualification'):adapter.qualify_route(status,manifest,installation,now=1000000,provider_attestation=provider)

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
            header={'type':'manifest','build_id':manifest['active_build'],'source_watermark':100,'actor_count':1,'source_actor_count':1,'normalize_version':manifest['normalize_version'],'source_database_sha256':manifest['source_database_sha256'],'source_prefix_sha256':manifest['source_prefix_sha256'],'partial':False}
            manifest['source_actors_sha256']=hashlib.sha256(lines).hexdigest()
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
            source={'manifest_version':2,'format_version':5,'active_build':manifest['source_build_id'],'source_watermark':100,'actor_count':1,'sha256':manifest['source_database_sha256'],'normalize_version':manifest['normalize_version'],'snapshot_url':f"releases/search/{manifest['source_build_id']}/index.db",'prefix_map':{'sha256':manifest['source_prefix_sha256']},'legacy_twin':{'build_id':manifest['legacy_build_id'],'source_watermark':100}}
            source_bytes=json.dumps(source).encode();manifest['source_manifest_sha256']=hashlib.sha256(source_bytes).hexdigest()
            portable=adapter.freeze_bundle(data,root/'published',manifest,source_bytes,binary)
            adapter.validate_artifact_manifest(portable,manifest['active_build'])
            self.assertNotIn('volume_id',portable)
            self.assertEqual(json.loads((root/'published/manifest.json').read_bytes()),portable)
            with self.assertRaisesRegex(ValueError,'byte identity'):adapter.validate_source_manifest(portable,source_bytes+b' ')
            for field,value in [('source_build_id','b1793236000-other'),('legacy_build_id','b1793236000-other'),('source_database_sha256','4'*64),('source_actor_count',2)]:
                with self.subTest(source=field),self.assertRaises(ValueError):adapter.validate_source_manifest(dict(portable,**{field:value}),source_bytes)
            with self.assertRaises(ValueError):adapter.freeze_bundle(data,root/'published',manifest,source_bytes,binary)
            with self.assertRaises(ValueError):adapter.freeze_bundle(data,root/'wrong-source',dict(manifest,source_actors_sha256='0'*64),source_bytes,binary)
            self.assertFalse((root/'wrong-source').exists())
            if shutil.which('rclone'):
                sys.path.insert(0,str(ROOT/'scripts/snapshot_compaction'))
                from publish import Publisher
                remote=root/'remote';source_path=root/'native-manifest.json';source_path.write_bytes(source_bytes)
                source_remote=remote/f"releases/search/{manifest['source_build_id']}/manifest.json"
                source_remote.parent.mkdir(parents=True);source_remote.write_bytes(source_bytes)
                pointer=remote/'latest.json';pointer.write_bytes(b'production untouched')
                config=root/'rclone.conf';config.write_text('[r2]\ntype = local\n')
                publisher=Publisher(config,str(remote))
                for _ in range(2):self.assertEqual(publisher.publish_tantivy(root/'published',source_path),manifest['active_build'])
                published_manifest=remote/f"releases/search-tantivy/{manifest['active_build']}/manifest.json"
                published_bytes=published_manifest.read_bytes();self.assertEqual(json.loads(published_bytes),portable)
                local_bundle=root/'published/index.tar';saved_bundle=root/'saved-index.tar';local_bundle.rename(saved_bundle)
                self.assertEqual(publisher.verify_tantivy(root/'published',source_path),manifest['active_build'])
                self.assertFalse(local_bundle.exists());saved_bundle.rename(local_bundle)
                source_remote.write_bytes(source_bytes+b' ')
                with self.assertRaisesRegex(ValueError,'remote verification'):publisher.publish_tantivy(root/'published',source_path)
                with self.assertRaisesRegex(ValueError,'remote verification'):publisher.verify_tantivy(root/'published',source_path)
                source_remote.write_bytes(source_bytes)
                (remote/portable['object_key']).write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError,'remote verification'):publisher.publish_tantivy(root/'published',source_path)
                self.assertEqual(published_manifest.read_bytes(),published_bytes);self.assertEqual(pointer.read_bytes(),b'production untouched')
            manifest.update(portable);bundle=root/'published/index.tar'
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

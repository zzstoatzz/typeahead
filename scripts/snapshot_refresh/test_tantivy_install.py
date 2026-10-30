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


def start_strict(binary,data,log,secret,fixture_identity=False,max_sync_lag=1800):
    public,admin=smoke.port(),smoke.port()
    environment=dict(os.environ,ADMIN_SECRET=secret)
    if fixture_identity:environment.update(FLY_MACHINE_ID='abcdef12345678',FLY_MACHINE_VERSION='fixture-instance')
    child=subprocess.Popen([str(binary),'serve','--data',str(data),'--listen',f'127.0.0.1:{public}','--admin-listen',f'127.0.0.1:{admin}','--max-sync-lag-s',str(max_sync_lag)],env=environment,stdout=log,stderr=log)
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
                  'snapshot_url':'releases/search/b1792000000-rollback-px05/index.db',
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
                            'checkpoint_receipt_sha256':receipt_sha,'checkpoint_receipt':receipt,'checkpoint_receipt_json':receipt_raw.decode(),
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
        single_pass=copy.deepcopy(manifest)
        del single_pass['source_bootstrap']['actors_sha256']
        self.assertTrue(adapter.validate_checkpoint_provenance(single_pass,receipt_raw,native_raw)['structurally_valid'])
        self.assertEqual(adapter.validate_source_manifest(manifest,native_raw),result)
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(manifest,receipt_raw+b' ',native_raw)
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(manifest,receipt_raw,native_raw+b' ')
        known=copy.deepcopy(manifest)
        known['source_bootstrap']['counts']['native_visible_actors']=1
        with self.assertRaises(ValueError):adapter.validate_checkpoint_provenance(known,receipt_raw,native_raw)
        known['source_bootstrap']['native_visible_database_sha256']=manifest['source_database_sha256']
        self.assertEqual(adapter.validate_checkpoint_provenance(known,receipt_raw,native_raw)['counts']['native_visible_actors'],1)

    def test_checkpoint_fetch_budget_accepts_nine_hundred_seconds_without_moving_source_floor(self):
        original,_,native_raw=self.checkpoint_manifest()
        for elapsed in [180.1,899.9,900]:
            changed=copy.deepcopy(original)
            bootstrap=changed['source_bootstrap'];receipt=bootstrap['checkpoint_receipt']
            receipt['wall_seconds']=elapsed
            raw=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()
            bootstrap['checkpoint_receipt_json']=raw.decode()
            bootstrap['checkpoint_receipt_sha256']=hashlib.sha256(raw).hexdigest()
            changed['source_manifest_sha256']=bootstrap['checkpoint_receipt_sha256']
            with self.subTest(elapsed=elapsed):
                self.assertTrue(adapter.validate_checkpoint_provenance(changed,raw,native_raw)['structurally_valid'])
                self.assertEqual(changed['source_watermark'],original['source_watermark'])
                self.assertEqual(receipt['source_clock_before'],original['source_bootstrap']['checkpoint_receipt']['source_clock_before'])
        for elapsed in [900.001,-1,True,float('inf'),float('nan')]:
            changed=copy.deepcopy(original)
            bootstrap=changed['source_bootstrap'];receipt=bootstrap['checkpoint_receipt']
            receipt['wall_seconds']=elapsed
            raw=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()
            bootstrap['checkpoint_receipt_json']=raw.decode()
            bootstrap['checkpoint_receipt_sha256']=hashlib.sha256(raw).hexdigest()
            changed['source_manifest_sha256']=bootstrap['checkpoint_receipt_sha256']
            with self.subTest(elapsed=elapsed),self.assertRaises(ValueError):
                adapter.validate_checkpoint_provenance(changed,raw,native_raw)

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

    def test_fixture_checkpoint_cannot_freeze_install_or_claim_runtime_in_production(self):
        manifest,_,_=self.checkpoint_manifest()
        with self.assertRaisesRegex(ValueError,'Fixture checkpoint is not eligible'):
            adapter.freeze_bundle('/missing/data','/missing/destination',manifest,b'','/missing/binary')
        with self.assertRaisesRegex(ValueError,'Fixture checkpoint is not eligible'):
            adapter.install_bundle('/missing/bundle','/missing/destination',manifest,'/missing/binary')
        with self.assertRaisesRegex(ValueError,'Fixture checkpoint is not eligible'):
            adapter.validate_runtime({'ready':True,'cutover_source_ready':True},manifest)
        with self.assertRaisesRegex(ValueError,'Fixture checkpoint is not eligible'):
            adapter.validate_metadata({},manifest,require_source_ready=False,require_coverage_ready=False)

    def checkpoint_status(self):
        manifest,_,_ = self.checkpoint_manifest()
        status = self.status()
        now = 1000040
        floor = manifest['source_watermark']
        status.update(snapshot_watermark=floor,watermark=now-5,input_actors=4,actor_count=2,tombstone_count=2,source_observation_revision=4,cutover_source_ready=False)
        status['source_manifest']={'type':'manifest','schema_version':1,'build_id':manifest['source_build_id'],
            'source_watermark':floor,'source_database_sha256':manifest['source_database_sha256'],
            'source_format':'sqlite-committed-prefix-v1','source_kind':adapter.CHECKPOINT_SOURCE,'source_policy':adapter.CHECKPOINT_POLICY,'rollback_native_anchor':copy.deepcopy(manifest['rollback_native_anchor']),'legacy_build_id':manifest['legacy_build_id'],'partial':False,'actor_count':4,'source_actor_count':4,
            'source_bootstrap':copy.deepcopy(manifest['source_bootstrap'])}
        status['expiry_coverage'].update(expected_input_actors=4,source_actor_count=4,input_actors_checked=4)
        status['expiry_coverage']['current']['live_actors']=2
        status['moderation']['source_observed_at']=now-5
        status['query_config']={'allow_partial':False,'allow_stale':False,'max_sync_lag_s':120,'retrieval_strategy':manifest['retrieval_strategy']}
        bootstrap_sha=adapter.digest_json(manifest['source_bootstrap'])
        record={'schema_version':1,'kind':'verified-checkpoint-import','bootstrap_receipt_sha256':bootstrap_sha,
                'actors_sha256':manifest['source_actors_sha256'],'verified_input_actors':4,'raw_actor_rows':3,
                'initial_live_actor_documents':2,'initial_deleted_actor_documents':2,
                'started_at':1000010,'completed_at':1000012}
        record_sha=adapter.digest_json(record)
        handoff={'schema_version':1,'kind':'sqlite-checkpoint-to-turso-v2','prior_cursor_sha256':adapter.digest_json(None),
                 'prior_observation_revision':0,'checkpoint_reconciliation_sha256':record_sha,
                 'bootstrap_receipt_sha256':bootstrap_sha,'source_origin_sha256':'4'*64,
                 'replay_floor':floor,'first_remote_through':1000025}
        status['source_cursor']={'schema_version':1,'source_policy':adapter.SOURCE_POLICY,'build_id':manifest['active_build'],
            'snapshot_watermark':floor,'through':now-5,'cycle':None,'lookback_seconds':300,
            'actors':{'updated_at':now-5,'did':''},'tombstones':{'deleted_at':now-5,'did':''},
            'checkpoint_reconciliation':record,'checkpoint_handoff':handoff,'reconciliation':{'schema_version':1}}
        manifest['checkpoint_reconciliation_sha256']=record_sha
        manifest['checkpoint_handoff_sha256']=adapter.digest_json(handoff)
        return manifest,status,now

    def test_checkpoint_runtime_requires_explicit_post_cut_remote_handoff_and_no_fixture_production(self):
        manifest,status,now=self.checkpoint_status()
        with self.assertRaisesRegex(ValueError,'Fixture checkpoint'):
            adapter.validate_runtime(status,manifest,now)
        gates=adapter.validate_runtime(status,manifest,now,allow_checkpoint_fixture=True)
        self.assertEqual(gates['source_coverage_kind'],adapter.CHECKPOINT_SOURCE)
        self.assertTrue(gates['checkpoint_test_only'])
        self.assertFalse(status['cutover_source_ready'])
        self.assertEqual(gates['coverage_deadline'],1000000+86400)
        cases=[(('full_source',),False),(('import_complete',),False),(('input_actors',),3),
               (('expiry_coverage','current','actors_with_unsupported_expiry'),1),
               (('expiry_coverage','verified_input_actors_sha256'),'0'*64),
               (('source_manifest','source_bootstrap','replay_floor'),999999),
               (('source_cursor','checkpoint_handoff','first_remote_through'),1000001),
               (('source_cursor','checkpoint_handoff','source_origin_sha256'),'0'*64),
               (('source_cursor','checkpoint_handoff','prior_observation_revision'),4),
               (('source_cursor','checkpoint_reconciliation','raw_actor_rows'),4),
               (('source_cursor','checkpoint_reconciliation','documents_checked'),8),
               (('source_cursor','cycle'),{'stream':'actors'}),
               (('source_cursor','actors','updated_at'),999999),
               (('source_cursor','source_policy'),adapter.CHECKPOINT_POLICY),
               (('query_config','allow_partial'),True),(('query_config','allow_stale'),True),
               (('query_config','max_sync_lag_s'),1800),(('moderation','source_observed_at'),1000001),
               (('cutover_source_ready',),True)]
        for path,value in cases:
            changed=copy.deepcopy(status);target=changed
            for key in path[:-1]:target=target[key]
            target[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(ValueError):
                adapter.validate_runtime(changed,manifest,now,allow_checkpoint_fixture=True)
        for flag in ['require_fresh','require_source_ready','require_coverage_ready','require_complete_poll']:
            with self.subTest(flag=flag),self.assertRaisesRegex(ValueError,'cannot disable'):
                adapter.validate_runtime(status,manifest,now,allow_checkpoint_fixture=True,**{flag:False})
        for bound,value in [('max_poll_age',121),('max_policy_age',301),('max_sweep_age',86401),('max_sweep_duration',86401)]:
            with self.subTest(bound=bound),self.assertRaises(ValueError):
                adapter.validate_runtime(status,manifest,now,allow_checkpoint_fixture=True,**{bound:value})

    def test_checkpoint_ordinary_poll_does_not_renew_coverage_and_local_markers_cannot_be_relabelled(self):
        manifest,status,now=self.checkpoint_status()
        later=now+90000
        status['watermark']=later-5
        status['source_cursor']['through']=later-5
        status['source_cursor']['actors']['updated_at']=later-5
        status['source_cursor']['tombstones']['deleted_at']=later-5
        status['moderation']['source_observed_at']=later-5
        with self.assertRaisesRegex(ValueError,'coverage.*stale'):
            adapter.validate_runtime(status,manifest,later,allow_checkpoint_fixture=True)
        status['source_cursor']['reconciliation']={'schema_version':1,'round':2,'source_after_did':'','backend_after_did':'','source_done':False,'backend_done':False,'round_started_at':1000012,'last_completed_started_at':999702,'last_completed_at':1000012}
        status['cutover_source_ready']=True
        with self.assertRaisesRegex(ValueError,'cannot be relabeled'):
            adapter.validate_runtime(status,manifest,later,allow_checkpoint_fixture=True)
        status['source_cursor']['reconciliation'].update(last_completed_started_at=later-20,last_completed_at=later-5,round_started_at=later-5)
        gates=adapter.validate_runtime(status,manifest,later,allow_checkpoint_fixture=True)
        self.assertTrue(gates['checkpoint_renewed_by_remote_round'])
        self.assertEqual(gates['coverage_deadline'],later-5+86400)

    def test_actual_checkpoint_wal_stream_import_catchup_and_test_only_admission(self):
        binary=Path(os.environ.get('TA_BINARY',ROOT/'services/tantivy/target/release/typeahead-tantivy')).resolve()
        if not binary.is_file():self.skipTest('actual Rust binary unavailable')
        with tempfile.TemporaryDirectory(prefix='checkpoint-runtime-') as temporary:
            with checkpoint_runtime_fixture(Path(temporary),binary) as case:
                destination=Path(temporary)/'installed'
                manifest=case['manifest']
                if shutil.which('rclone'):
                    sys.path.insert(0,str(ROOT/'scripts/snapshot_compaction'))
                    from publish import Publisher
                    root=Path(temporary);remote=root/'remote'
                    source_path=root/'native-anchor.json';source_path.write_bytes(case['native'])
                    native_build,_=adapter.native_anchor_binding(manifest)
                    retained=remote/f'releases/search/{native_build}/manifest.json'
                    retained.parent.mkdir(parents=True);retained.write_bytes(case['native'])
                    pointer=remote/'control/builds/latest.json';pointer.parent.mkdir(parents=True);pointer.write_bytes(b'unchanged native publisher')
                    config=root/'rclone.conf';config.write_text('[r2]\ntype = local\n')
                    publisher=Publisher(config,str(remote));generation=case['bundle'].parent
                    for _ in range(2):self.assertEqual(publisher.publish_tantivy(generation,source_path),manifest['active_build'])
                    self.assertFalse((remote/f"releases/search/{manifest['source_build_id']}").exists())
                    self.assertEqual(publisher.verify_tantivy(generation,source_path),manifest['active_build'])
                    retained.write_bytes(case['native']+b' ')
                    with self.assertRaisesRegex(ValueError,'remote verification'):publisher.verify_tantivy(generation,source_path)
                    self.assertEqual(pointer.read_bytes(),b'unchanged native publisher')
                installation=adapter.install_bundle(case['bundle'],destination,manifest,binary,allow_checkpoint_fixture=True)
                child,public,_=start_strict(binary,destination,case['log'],case['secret'],fixture_identity=True,max_sync_lag=120)
                try:
                    status=smoke.request(public,'/index/status')[2]
                    self.assertFalse(status['cutover_source_ready'])
                    provider=self.provider(int(time.time()))
                    qualified=adapter.qualify_runtime(public,manifest,installation,allow_loopback=True,provider_attestation=provider)
                    self.assertTrue(qualified['admission']['test_only'])
                    self.assertEqual(qualified['admission']['source_coverage_kind'],adapter.CHECKPOINT_SOURCE)
                    self.assertEqual(smoke.request(public,'/search?q=real')[2]['actors'][0]['handle'],'real.test')
                    with self.assertRaisesRegex(ValueError,'Fixture checkpoint'):
                        adapter.validate_runtime(status,manifest)
                    self.assertEqual(status['source_manifest']['source_bootstrap'],manifest['source_bootstrap'])
                finally:smoke.stop(child)

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



from contextlib import contextmanager

@contextmanager
def checkpoint_runtime_fixture(root,binary,slot_app=None):
    import sqlite3
    import struct
    mps_root=os.environ.get('TA_MPS_REPOSITORY')
    if not mps_root:raise unittest.SkipTest('requires existing MPS checkpoint producer helpers')
    sys.path.insert(0,str(Path(mps_root)/'packages/mps/src'))
    from mps.typeahead_checkpoint import fetch_checkpoint
    from mps.typeahead_checkpoint_stream import stream_checkpoint,file_witness
    sys.path.insert(0,str(ROOT/'services/tantivy-input'))
    import test_sync
    import sync as replay
    import current_state
    root=root.resolve();root.mkdir(parents=True,exist_ok=True)
    provider_root=root/'provider';provider_root.mkdir()
    source=test_sync.Fixture(provider_root,now=int(time.time())-4)
    keeper=sqlite3.connect(source.path)
    child=None
    log=(root/'checkpoint-reader.log').open('wb')
    try:
        source.actor('did:plc:fixture000001',source.now-1,handle='real.test')
        keeper.execute('PRAGMA journal_mode=WAL');keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        base=source.path.read_bytes()
        source.actor('did:plc:fixture000002',source.now-1,handle='second.test')
        keeper.execute('UPDATE actors SET followers_count=12345 WHERE did=?',('did:plc:fixture000001',));keeper.commit()
        wal=Path(str(source.path)+'-wal').read_bytes();frames=wal[32:]
        original_handler=source.server.RequestHandlerClass
        class Combined(original_handler):
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer '+source.source_secret:self.send_error(401);return
                if self.path=='/info':raw=json.dumps({'current_generation':7}).encode()
                elif self.path=='/export/7':raw=base
                elif self.path.startswith('/sync/7/'):
                    start,end=map(int,self.path.split('/')[-2:]);raw=frames[(start-1)*4120:(end-1)*4120]
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        source.server.RequestHandlerClass=Combined
        database=root/'materialized.db'
        checkpoint=fetch_checkpoint(source.url,source.source_secret,database,max_seconds=5,max_bytes=1024*1024,max_wal_bytes=1024*1024,allow_test_loopback=True)
        if not checkpoint['sqlite_replay_verified'] or checkpoint['committed_frame']<1:raise AssertionError('real committed WAL was not replayed')
        raw_receipt=json.dumps(checkpoint,sort_keys=True,separators=(',',':')).encode()+b'\n'
        manifest,_,native=AdmissionTests().checkpoint_manifest()
        if slot_app is not None:manifest.update(provider_app=slot_app,public_service_ports=[80,443])
        manifest['binary_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
        seed=root/'source.jsonl';started=int(time.time())
        with seed.open('wb') as output:
            stream=stream_checkpoint(database,output,ROOT,receipt_bytes=raw_receipt,receipt_sha256=hashlib.sha256(raw_receipt).hexdigest(),
                expected_source_origin_sha256=checkpoint['source_origin_sha256'],expected_file_witness=file_witness(database),
                native_manifest_bytes=native,native_manifest_sha256=hashlib.sha256(native).hexdigest(),
                build_id=manifest['active_build'],max_seconds=30,allow_fixture=True)
        header=stream['source_manifest'];bootstrap=header['source_bootstrap'];counts=bootstrap['counts']
        manifest.update(source_build_id=header['build_id'],source_watermark=header['source_watermark'],source_actor_count=stream['input_documents'],
            source_database_sha256=checkpoint['database_sha256'],source_manifest_sha256=hashlib.sha256(raw_receipt).hexdigest(),
            source_actors_sha256=stream['actors_sha256'],source_bootstrap=bootstrap,rollback_native_anchor=header['rollback_native_anchor'],
            legacy_build_id=header['legacy_build_id'])
        data=root/'data'
        result=subprocess.run([str(binary),'import','--data',str(data),'--input',str(seed)],capture_output=True,timeout=30)
        if result.returncode:raise AssertionError(result.stderr[-2000:])
        metadata=json.loads(subprocess.check_output([str(binary),'status','--data',str(data)],timeout=30))
        if (metadata['input_actors'],metadata['actor_count'],metadata['tombstone_count'])!=(counts['input_documents'],counts['live_actor_documents'],counts['deleted_actor_documents']):raise AssertionError('actual import counts differ')
        record={'schema_version':1,'kind':'verified-checkpoint-import','bootstrap_receipt_sha256':adapter.digest_json(bootstrap),
            'actors_sha256':stream['actors_sha256'],'verified_input_actors':metadata['input_actors'],'raw_actor_rows':counts['raw_actor_rows'],
            'initial_live_actor_documents':metadata['actor_count'],'initial_deleted_actor_documents':metadata['tombstone_count'],
            'started_at':started,'completed_at':int(time.time())}
        handoff={'schema_version':1,'kind':'sqlite-checkpoint-to-turso-v2','prior_cursor_sha256':adapter.digest_json(None),
            'prior_observation_revision':0,'checkpoint_reconciliation_sha256':adapter.digest_json(record),
            'bootstrap_receipt_sha256':adapter.digest_json(bootstrap),'source_origin_sha256':checkpoint['source_origin_sha256'],
            'replay_floor':manifest['source_watermark'],'first_remote_through':None}
        secret=secrets.token_urlsafe(24)
        child,public,admin_url=start_strict(binary,data,log,secret,fixture_identity=True,max_sync_lag=120)
        admin=replay.Admin(admin_url,secret,public+'/index/status');writer=current_state.ObservationAdmin(admin,replay)
        state,revision=writer.state([])
        cursor={'schema_version':1,'build_id':manifest['active_build'],'snapshot_watermark':manifest['source_watermark'],'source_policy':adapter.SOURCE_POLICY,
            'through':manifest['source_watermark'],'cycle':None,'lookback_seconds':300,
            'actors':{'updated_at':manifest['source_watermark'],'did':''},'tombstones':{'deleted_at':manifest['source_watermark'],'did':''},
            'checkpoint_reconciliation':record,'checkpoint_handoff':handoff}
        writer.reconcile([],[],revision,state['source_cursor'],cursor)
        source.now=int(time.time())
        observed=current_state.ObservedSynchronizer(source.source(),admin,replay,policy_mode='snapshot',anti_entropy_pages=0,safety_lag=0,max_pages=10,max_seconds=10,retries=1,backoff=0).run()
        if not observed['caught_up']:raise AssertionError('actual remote poll did not drain')
        state,revision=writer.state([]);cursor=copy.deepcopy(state['source_cursor'])
        cursor['checkpoint_handoff']['first_remote_through']=cursor['through']
        writer.reconcile([],[],revision,state['source_cursor'],cursor)
        manifest['checkpoint_reconciliation_sha256']=adapter.digest_json(record)
        manifest['checkpoint_handoff_sha256']=adapter.digest_json(cursor['checkpoint_handoff'])
        status=smoke.request(public,'/index/status')[2]
        if status['cutover_source_ready'] is not False:raise AssertionError('checkpoint falsely advertised traditional remote round')
        adapter.validate_runtime(status,manifest,allow_checkpoint_fixture=True)
        smoke.stop(child);child=None
        publication=root/'publication'
        portable=adapter.freeze_bundle(data,publication,manifest,native,binary,allow_checkpoint_fixture=True)
        manifest.update(portable)
        yield {'manifest':manifest,'portable':portable,'bundle':publication/'index.tar','native':native,'source':source,'secret':secret,
               'checkpoint':checkpoint,'record':record,'binary':binary,'log':log,'data':data,'replay':replay,'current':current_state}
    finally:
        if child is not None:smoke.stop(child)
        keeper.close();source.close();log.close()


if __name__=='__main__':unittest.main()

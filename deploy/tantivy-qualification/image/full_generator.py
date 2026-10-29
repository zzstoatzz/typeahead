"""Private full-corpus performance component; never a production cutover decision."""
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import socket
import struct
import threading
import time
import urllib.parse

APP='typeahead-qual-20261029'
SHAPE={'cpu_kind':'shared','cpus':2,'memory_mb':1024}
HEX=re.compile(r'[0-9a-f]{64}\Z')
TERMINAL={'destroyed','failed','launch_failed'}
PHASES=(('first_touch',512,10),('warm',1200,10),('mixed',3000,10))

class QualificationError(Exception):pass
class ObservationError(Exception):pass
class NotReady(Exception):pass

def require(condition,code):
    if not condition:raise QualificationError(code)

def digest(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def sha(value):return isinstance(value,str) and HEX.fullmatch(value) is not None
def integer(value):return type(value) is int

def origin(value,port,fixture=False):
    parsed=urllib.parse.urlsplit(value)
    host=parsed.hostname or ''
    local=fixture and host in {'127.0.0.1','::1'}
    match=re.fullmatch(r'([0-9a-f]+)\.vm\.'+re.escape(APP)+r'\.internal',host)
    require(parsed.scheme=='http' and (local or match is not None) and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path in {'','/'} and (local or parsed.port==port),'owned_private_origin_required')
    return host,parsed.port or port,None if local else match.group(1)

class Client:
    def __init__(self,url,port,secret=None,fixture=False):
        self.host,self.port,self.machine=origin(url,port,fixture)
        self.secret=secret
        addresses=socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)
        require(bool(addresses),'private_dns_unavailable')
        self.address=addresses[0][4][0]
    def request(self,path,method='GET',limit=524288,deadline=None,sink=None):
        require(path.startswith('/') and not path.startswith('//') and '\r' not in path and '\n' not in path,'relative_request_path')
        deadline=deadline or time.monotonic()+3
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('queue_deadline')
        connection=http.client.HTTPConnection(self.address,self.port,timeout=remaining)
        active=[None];response=None
        def abort():
            sock=active[0] or connection.sock
            if sock is not None:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                sock.close()
        timer=threading.Timer(remaining,abort);timer.daemon=True;timer.start()
        try:
            connection.connect();active[0]=connection.sock
            if time.monotonic()>=deadline:raise TimeoutError('connect_deadline')
            headers={'Host':self.host}
            if self.secret is not None:headers['X-Pilot-Authorization']=self.secret
            connection.request(method,path,body=None,headers=headers)
            response=connection.getresponse();raw=bytearray();size=0;checksum=hashlib.sha256()
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('request_deadline')
                if active[0] is not None and active[0].fileno()>=0:active[0].settimeout(remaining)
                part=response.read1(min(65536,limit+1-size))
                if not part:break
                size+=len(part);require(size<=limit,'response_byte_budget');checksum.update(part)
                if sink is None:raw.extend(part)
                else:sink.write(part)
            if time.monotonic()>deadline:raise TimeoutError('request_deadline')
            return response.status,{k.lower():v for k,v in response.getheaders()},json.loads(raw) if sink is None else None,checksum.hexdigest()
        finally:
            timer.cancel()
            if response is not None:response.close()
            connection.close()
    def download(self,path,target,expected,limit):
        try:
            with Path(target).open('xb') as output:
                status,_,_,checksum=self.request(path,limit=limit,deadline=time.monotonic()+15,sink=output)
            require(status==200,'input_http_status');require(checksum==expected,'input_content_hash')
            with Path(target).open('r',encoding='utf8') as stream:return json.load(stream),checksum
        except QualificationError as error:
            if str(error)=='response_byte_budget':raise QualificationError('input_resource_cap_'+Path(target).stem) from None
            raise

    def ok(self,path,method='GET',limit=524288,seconds=15):
        status,_,value,checksum=self.request(path,method,limit,time.monotonic()+seconds)
        if status!=200:
            code=value.get('error') if isinstance(value,dict) else None
            suffix='_'+code if isinstance(code,str) and re.fullmatch('[A-Za-z0-9_]{1,80}',code) else ''
            raise ObservationError('http_'+str(status)+suffix)
        return value,checksum

def provider_state(machine,capability=None):
    if not capability:return None
    require(re.fullmatch('[0-9a-f]+',machine or '') is not None,'provider_machine_id')
    connection=http.client.HTTPSConnection('api.machines.dev',timeout=3)
    try:
        connection.request('GET','/v1/apps/'+APP+'/machines/'+machine,headers={'Authorization':'Bearer '+capability})
        response=connection.getresponse();raw=response.read(262145)
        if response.status!=200 or len(raw)>262144:return None
        value=json.loads(raw)
        return value.get('state') if value.get('id')==machine else None
    except (OSError,ValueError,http.client.HTTPException):return None
    finally:connection.close()

def provider_record(kind,identity,capability):
    require(kind in {'machines','volumes'} and re.fullmatch('[A-Za-z0-9_]+',identity or '') is not None,'provider_resource_id')
    if not capability:raise ObservationError('provider_capability_unavailable')
    connection=http.client.HTTPSConnection('api.machines.dev',timeout=3)
    try:
        started=time.monotonic()
        connection.request('GET','/v1/apps/'+APP+'/'+kind+'/'+identity,headers={'Authorization':'Bearer '+capability})
        response=connection.getresponse();raw=bytearray()
        while True:
            remaining=3-(time.monotonic()-started)
            if remaining<=0:raise TimeoutError('provider_deadline')
            if connection.sock:connection.sock.settimeout(remaining)
            part=response.read1(65536)
            if not part:break
            raw.extend(part)
            if len(raw)>1048576:raise ObservationError('provider_response_budget')
        if response.status!=200:raise ObservationError('provider_observation_unavailable')
        return json.loads(raw)
    except (OSError,ValueError,http.client.HTTPException):raise ObservationError('provider_observation_unavailable') from None
    finally:connection.close()

def verify_provider(arm,prepared,evaluation):
    image=os.environ.get('QUAL_IMAGE_DIGEST','');run=os.environ.get('QUAL_RUN_ID','')
    require(re.fullmatch(re.escape('registry.fly.io/'+APP+'@sha256:')+'[a-f0-9]{64}',image) is not None and re.fullmatch('[a-f0-9]{8,16}',run) is not None,'provider_expected_image_run')
    token=os.environ.get('QUAL_REAPER_CAPABILITY')
    machine=provider_record('machines',prepared['machine_id'],token)
    volume=provider_record('volumes',prepared['volume_id'],token)
    return validate_provider_records(arm,prepared,evaluation,machine,volume,image,run)

def validate_provider_records(arm,prepared,evaluation,machine,volume,image,run):
    config=machine.get('config',{})
    require(machine.get('id')==prepared['machine_id'] and machine.get('state')=='started','provider_running_machine')
    require(all(config.get('metadata',{}).get(key)==value for key,value in {'owner':'tantivy-qualification-20261029','run_id':run,'role':arm+'-control'}.items()),'provider_owner')
    require(config.get('image')==image and config.get('guest')==SHAPE and not config.get('services'),'provider_image_guest_services')
    require(not any(key in config.get('env',{}) for key in ['FLY_MACHINE_ID','FLY_MACHINE_VERSION']),'provider_reserved_identity_override')
    mounts=config.get('mounts') or []
    require(len(mounts)==1 and mounts[0].get('volume')==prepared['volume_id'] and mounts[0].get('path')=='/data','provider_volume_mount')
    require(volume.get('id')==prepared['volume_id'] and volume.get('size_gb')==20 and volume.get('encrypted') is True and volume.get('attached_machine_id')==machine['id'],'provider_volume_shape')
    require(evaluation.get('runtime_machine_id')==machine['id'] and evaluation.get('runtime_instance_id')==machine.get('instance_id') and bool(machine.get('instance_id')),'provider_runtime_instance')
    return {'observed_at':time.time(),'app':APP,'machine_id':machine['id'],'instance_id':machine['instance_id'],'image_digest':image,'guest':config['guest'],'volume_id':volume['id'],'volume_gib':volume['size_gb'],'encrypted':True,'public_services':[]}

def wait_prepared(arms,until,nonce,provider=None,pause=2):
    observations={};last_provider={};failures={arm:0 for arm in arms}
    while time.time()<until:
        for arm,client in arms.items():
            if arm in observations:continue
            try:
                value,_=client.ok('/prepared',seconds=3)
                require(value.get('arm')==arm and value.get('nonce')==nonce,'prepared_identity')
                if value.get('prepared') is True:
                    observations[arm]=value;continue
                require(value.get('state') not in {'failed','cleanup_failed'},'preparation_failed')
            except (OSError,ValueError,ObservationError,http.client.HTTPException):
                failures[arm]+=1
                if provider and time.monotonic()-last_provider.get(arm,-100)>5:
                    last_provider[arm]=time.monotonic()
                    state=provider(client.machine)
                    require(state not in TERMINAL,'provider_terminal_'+str(state))
        if len(observations)==len(arms):return observations,failures
        time.sleep(min(pause,max(0,until-time.time())))
    raise QualificationError('preparation_deadline_observation_unknown')

def validate_prepared(values,nonce,fixture=False):
    for arm,value in values.items():
        require(value.get('mode')=='full' and value.get('arm')==arm and value.get('prepared') is True and value.get('nonce')==nonce,'prepared_identity')
        require(value.get('guest')==SHAPE and value.get('final_volume_gib')==20 and value.get('public_services')==[],'prepared_resource_shape')
        require(value.get('source_ready_attestation') is False and value.get('source_kind')=='frozen-release-snapshot','frozen_source_scope')
        require(value.get('affected_actors')==64 and value.get('feed_batches')==9,'prepared_feed_shape')
        for key in ['corpus_sha256','updates_sha256','observer_receipt_sha256']:require(sha(value.get(key)),'prepared_hash_'+key)
        require(integer(value.get('actor_count')) and value['actor_count']>0 and integer(value.get('source_watermark')),'prepared_source_shape')
        require(isinstance(value.get('build_id'),str) and value['build_id'],'prepared_build')
        if not fixture:require(value.get('app')==APP,'prepared_app')
    left,right=values['native'],values['tantivy']
    for key in ['corpus_sha256','updates_sha256','build_id','source_watermark','actor_count']:require(left[key]==right[key],'arm_input_mismatch_'+key)
    require(left.get('machine_id')!=right.get('machine_id') and left.get('volume_id')!=right.get('volume_id'),'arm_resource_isolation')
    require(left.get('oracle_ready') is True and sha(left.get('cases_sha256')) and sha(left.get('oracle_sha256')),'native_oracle_not_ready')

def load_inputs(control,prepared,expected_oracle,root,fixture=False):
    require(sha(expected_oracle),'expected_oracle_hash')
    directory=Path(root)/('inputs-'+str(time.monotonic_ns()));directory.mkdir(mode=0o700)
    cases,cases_sha=control.download('/inputs/cases',directory/'cases.json',prepared['cases_sha256'],4*1024*1024)
    oracle,oracle_sha=control.download('/inputs/oracle',directory/'oracle.json',prepared['oracle_sha256'],16*1024*1024)
    feed,feed_sha=control.download('/inputs/updates',directory/'updates.json',prepared['updates_sha256'],8*1024*1024)
    require(cases_sha==prepared['cases_sha256'] and oracle_sha==prepared['oracle_sha256'] and feed_sha==prepared['updates_sha256'],'input_content_hash')
    require(cases.get('schema_version')==1 and cases.get('source_kind')=='frozen-materialized-hidden','cases_schema')
    rows=cases.get('cases');require(isinstance(rows,list) and (1<=len(rows)<=512 if fixture else len(rows)==512),'case_count')
    ids=set()
    for case in rows:
        require(isinstance(case,dict) and isinstance(case.get('id'),str) and case['id'] not in ids,'case_identity');ids.add(case['id'])
        require(isinstance(case.get('q'),str) and integer(case.get('limit')) and 1<=case['limit']<=100 and type(case.get('native_shared_features')) is bool,'case_contract')
        require(len(case['q'].encode())<=512,'case_query_budget')
    require(any(case['native_shared_features'] for case in rows),'shared_cases_required')
    require(feed.get('schema_version')==1 and feed.get('build_id')==prepared['build_id'] and feed.get('source_watermark')==prepared['source_watermark'] and feed.get('source_kind')=='frozen-materialized-hidden','feed_source_binding')
    require(len(feed.get('baseline_actors',[]))==64 and [b.get('cursor') for b in feed.get('batches',[])]==list(range(1,10)),'feed_shape')
    require(all(b.get('expected_cursor')==i and len(b.get('actors',[]))==64 for i,b in enumerate(feed['batches'])),'feed_batch_shape')
    require(oracle.get('schema_version')==1,'oracle_schema')
    receipt=oracle.get('receipt',{})
    for field,target in [('source_build_id',prepared['build_id']),('source_watermark',prepared['source_watermark']),('actors_checked',prepared['actor_count']),('source_actor_count',prepared['actor_count']),('queries_sha256',cases_sha),('updates_sha256',feed_sha),('source_kind','frozen-materialized-hidden')]:require(receipt.get(field)==target,'oracle_source_'+field)
    for key in ['raw_source_sha256','actor_stream_sha256','identity_policy_sha256','oracle_binary_sha256']:require(sha(receipt.get(key)),'oracle_hash_'+key)
    require(receipt['oracle_binary_sha256']==expected_oracle,'oracle_binary_differs')
    require(receipt.get('live_policy_or_freshness_qualification') is False and receipt.get('expected_ids_derived_from_engine_responses') is False,'oracle_scope')
    checkpoints=oracle.get('checkpoints');require(isinstance(checkpoints,list) and [c.get('cursor') for c in checkpoints]==list(range(10)),'oracle_cursors')
    truth={}
    for checkpoint in checkpoints:
        answers=checkpoint.get('queries');require(isinstance(answers,dict) and set(answers)==ids,'oracle_case_set')
        for case in rows:
            answer=answers[case['id']]
            require(integer(answer.get('status')) and answer['status'] in {200,400} and isinstance(answer.get('ids'),list),'oracle_answer_type')
            require(all(isinstance(did,str) and did.startswith('did:') for did in answer['ids']) and len(answer['ids'])<=case['limit'] and len(set(answer['ids']))==len(answer['ids']),'oracle_ids')
            require(answer.get('native_shared_features')==case['native_shared_features'],'oracle_shared_features')
        truth[checkpoint['cursor']]=answers
    return rows,truth,{key:receipt[key] for key in ['raw_source_sha256','actor_stream_sha256','identity_policy_sha256','oracle_binary_sha256']}

class State:
    def __init__(self):self.lock=threading.Lock();self.acked=0;self.inflight=None
    def get(self):
        with self.lock:return self.acked,self.inflight
    def begin(self,cursor):
        with self.lock:
            require(self.inflight is None and cursor==self.acked+1,'client_cursor_order');self.inflight=cursor
    def commit(self,cursor):
        with self.lock:
            require(self.inflight==cursor,'client_cursor_ack');self.acked=cursor;self.inflight=None

def query_path(case):
    keys=['q','limit','pds','prefer_pds','preference','boost']
    return '/search?'+urllib.parse.urlencode({key:case[key] for key in keys if key in case})

def check_answer(status,headers,value,case,truth,minimum,ceiling,arm=None):
    low=headers.get('x-qualification-cursor-min');high=headers.get('x-qualification-cursor-max')
    require(isinstance(low,str) and low.isascii() and low.isdigit() and isinstance(high,str) and high.isascii() and high.isdigit(),'missing_or_invalid_cursor_window')
    low,high=int(low),int(high)
    require(minimum<=low<=high<=ceiling<=9,'cursor_window_outside_observed_state')
    expected=[truth[cursor][case['id']] for cursor in range(low,high+1)]
    if status!=expected[0]['status']:return 'unexpected_status'
    if status==200:
        if arm=='tantivy' or case['native_shared_features']:
            if case.get('pds'):
                host=urllib.parse.urlsplit(case['pds'] if '://' in case['pds'] else '//'+case['pds']).hostname
                if headers.get('x-typeahead-pds')!=host.rstrip('.').lower():return 'wrong_scope_ack'
        if arm=='tantivy' and case.get('prefer_pds'):
            host=urllib.parse.urlsplit(case['prefer_pds'] if '://' in case['prefer_pds'] else '//'+case['prefer_pds']).hostname
            mode=case.get('preference') or ('boost' if 'boost' in case else 'pds_first')
            if headers.get('x-typeahead-prefer-pds')!=host.rstrip('.').lower() or headers.get('x-typeahead-preference')!=mode:return 'wrong_preference_ack'
            if mode=='boost':
                boost=float(case['boost']);bits=struct.unpack('<I',struct.pack('<f',0.0 if boost==0 else boost))[0]
                if headers.get('x-typeahead-boost')!=f'{bits:08x}':return 'wrong_boost_ack'
        actors=value.get('actors') if isinstance(value,dict) else None
        require(isinstance(actors,list) and len(actors)<=case['limit'],'actor_response_shape')
        ids=[actor.get('did') if isinstance(actor,dict) else None for actor in actors]
        require(all(isinstance(did,str) and did.startswith('did:') for did in ids) and len(set(ids))==len(ids),'actor_identity_shape')
        return None if any(answer['status']==status and answer['ids']==ids for answer in expected) else 'wrong_ids'
    return None

class Detail:
    def __init__(self,path,maximum=64*1024*1024):self.stream=Path(path).open('xb');self.bytes=0;self.maximum=maximum;self.lock=threading.Lock()
    def write(self,value):
        raw=encoded(value)+b'\n'
        with self.lock:
            require(self.bytes+len(raw)<=self.maximum,'detail_byte_budget');self.stream.write(raw);self.bytes+=len(raw)
    def close(self):self.stream.flush();self.stream.close()

class Metrics:
    def __init__(self,client,path,interval=2):self.client=client;self.detail=Detail(path);self.interval=interval;self.stop=threading.Event();self.samples=[];self.errors=0;self.thread=None
    def sample(self):
        try:
            value,_=self.client.ok('/metrics',seconds=3);telemetry=value.get('telemetry')
            require(isinstance(telemetry,dict) and telemetry.get('schema_version')==1,'metrics_schema')
            require(len(self.samples)<512,'metrics_sample_budget')
            telemetry=dict(telemetry,qualification_free_bytes=value.get('free_bytes'),observed_monotonic=time.monotonic())
            self.detail.write(value);self.samples.append(telemetry)
        except (OSError,ValueError,QualificationError,ObservationError,http.client.HTTPException):self.errors+=1
    def start(self):
        self.sample()
        def loop():
            while not self.stop.wait(self.interval):self.sample()
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(4)
        require(self.thread is None or not self.thread.is_alive(),'metrics_thread_deadline')
        self.sample();self.detail.close()
        return summarize_metrics(self.samples)|{'sample_failures':self.errors,'raw_detail_bytes':self.detail.bytes}

def number(value):return type(value) in {int,float} and math.isfinite(value)
def at(value,*keys):
    for key in keys:
        if not isinstance(value,dict):return None
        value=value.get(key)
    return value if number(value) else None

def at_dict(value,*keys):
    for key in keys:
        if not isinstance(value,dict):return {}
        value=value.get(key)
    return value if isinstance(value,dict) else {}

def memory_io_observation(samples):
    def nonnegative(value):return number(value) and value>=0
    def all_fields(value,keys):return isinstance(value,dict) and all(nonnegative(value.get(key)) for key in keys)
    enough=len(samples)>=2
    guest_memory=enough and all(all_fields(at_dict(row,'guest_meminfo_bytes'),['MemTotal','MemAvailable','Cached','Dirty','Writeback']) for row in samples)
    processes=enough
    for sample in samples:
        rows=sample.get('processes') or []
        live=[row for row in rows if row.get('present') is True]
        processes &= sample.get('process_selection_complete') is True and bool(live) and not any(row.get('present') is None for row in rows)
        for row in live:
            processes &= integer(row.get('pid')) and integer(row.get('start_ticks')) and all_fields(row.get('memory_bytes'),['VmRSS','RssAnon','RssFile','RssShmem']) and all_fields(row.get('cpu_ticks'),['user','system']) and all_fields(row.get('io_bytes'),['read_bytes','write_bytes']) and nonnegative(row.get('major_faults'))
    guest_cpu=enough and all(all_fields(at_dict(row,'guest_cpu_ticks'),['user','nice','system','idle','iowait','irq','softirq','steal']) for row in samples)
    def devices(sample):
        rows=sample.get('guest_diskstats')
        if not isinstance(rows,list) or not rows:return None
        result={}
        for row in rows:
            if not all_fields(row,['read_bytes','write_bytes','io_milliseconds','weighted_io_milliseconds']) or not integer(row.get('major')) or not integer(row.get('minor')) or not isinstance(row.get('device'),str):return None
            result[(row['major'],row['minor'],row['device'])]=row
        return result
    disk_rows=[devices(row) for row in samples]
    guest_io=enough and all(row is not None for row in disk_rows)
    if guest_io:
        common=set(disk_rows[0]).intersection(*[set(row) for row in disk_rows[1:]])
        guest_io=bool(common) and all(b[device][key]>=a[device][key] for a,b in zip(disk_rows,disk_rows[1:]) for device in common for key in ['read_bytes','write_bytes','io_milliseconds','weighted_io_milliseconds'])
    cg_missing=[at(row,'cgroup','memory_current_bytes') is None and not at_dict(row,'cgroup','memory_stat') for row in samples]
    cg_complete=enough and all(nonnegative(at(row,'cgroup','memory_current_bytes')) and all_fields(at_dict(row,'cgroup','memory_stat'),['anon','file','shmem','file_mapped']) for row in samples)
    cg_mode='cgroup_memory' if cg_complete else 'guest_memory_fallback' if enough and all(cg_missing) else 'incomplete_cgroup_memory'
    checks={'guest_memory':bool(guest_memory),'selected_process_memory_cpu_io':bool(processes),'guest_cpu':bool(guest_cpu),'guest_block_io':bool(guest_io),'memory_accounting':cg_mode!='incomplete_cgroup_memory'}
    optional={}
    for name,path in [('guest_io_psi',('guest_io_pressure','some','total')),('guest_memory_psi',('guest_memory_pressure','some','total')),('cgroup_cpu_usage',('cgroup','cpu_stat','usage_usec')),('cgroup_cpu_throttling',('cgroup','cpu_stat','throttled_usec'))]:
        optional[name]=enough and all(nonnegative(at(row,*path)) for row in samples)
    return {'qualified':all(checks.values()),'checks':checks,'missing_required':[key for key,passed in checks.items() if not passed],
            'memory_accounting_mode':cg_mode,'optional_observations':optional,
            'scope':'guest and selected processes; RSS may double count shared pages; no cgroup usage inferred when unavailable',
            'directory_footprint_scope':'configured paths only; excludes unconfigured immutable source files and SQLite WAL siblings'}

def summarize_metrics(samples):
    def extrema(path,operation):
        values=[at(sample,*path) for sample in samples];values=[v for v in values if v is not None]
        return operation(values) if values else None
    def delta(path):
        if len(samples)<2:return None
        first,last=at(samples[0],*path),at(samples[-1],*path)
        return last-first if first is not None and last is not None and last>=first else None
    result={'samples':len(samples),'summed_rss_peak_bytes':extrema(('summed_process_rss_bytes',),max),
        'summed_rss_may_double_count_shared_pages':True,'cgroup_memory_peak_bytes':extrema(('cgroup','memory_current_bytes'),max),
        'guest_available_min_bytes':extrema(('guest_meminfo_bytes','MemAvailable'),min),'guest_cache_min_bytes':extrema(('guest_meminfo_bytes','Cached'),min),
        'guest_cache_peak_bytes':extrema(('guest_meminfo_bytes','Cached'),max),'guest_major_faults_delta':delta(('guest_vmstat','pgmajfault')),
        'cgroup_usage_usec_delta':delta(('cgroup','cpu_stat','usage_usec')),'cgroup_throttled_usec_delta':delta(('cgroup','cpu_stat','throttled_usec')),
        'cgroup_nr_throttled_delta':delta(('cgroup','cpu_stat','nr_throttled')),'guest_steal_ticks_delta':delta(('guest_cpu_ticks','steal')),
        'oom_kill_delta':delta(('guest_vmstat','oom_kill')),'psi_total_microseconds_delta':{},'block_device_deltas':[],
        'merge_observation':'directory segment and temporary-file counts are heuristics; no completed-merge claim',
        'memory_io_observation':memory_io_observation(samples)}
    result.update(swap_in_pages_delta=delta(('guest_vmstat','pswpin')),swap_out_pages_delta=delta(('guest_vmstat','pswpout')),
                  free_disk_min_bytes=extrema(('qualification_free_bytes',),min))
    times=[at(sample,'observed_monotonic') for sample in samples]
    result['maximum_sample_gap_seconds']=max((b-a for a,b in zip(times,times[1:]) if a is not None and b is not None),default=None)
    for scope in ['guest','cgroup']:
        for kind in ['io','memory']:
            for mode in ['some','full']:
                path=(f'guest_{kind}_pressure',mode,'total') if scope=='guest' else ('cgroup',kind+'_pressure',mode,'total')
                result['psi_total_microseconds_delta'][f'{scope}_{kind}_{mode}']=delta(path)
    if len(samples)>=2:
        first={str((r.get('major'),r.get('minor'),r.get('device'))):r for r in samples[0].get('guest_diskstats') or []}
        for row in samples[-1].get('guest_diskstats') or []:
            before=first.get(str((row.get('major'),row.get('minor'),row.get('device'))),{})
            result['block_device_deltas'].append({'device':row.get('device'),**{key:row[key]-before[key] if number(row.get(key)) and number(before.get(key)) and row[key]>=before[key] else None for key in ['read_bytes','write_bytes','io_milliseconds','weighted_io_milliseconds']}})
    cpu_keys=['user','nice','system','idle','iowait','irq','softirq','steal']
    cpu_deltas=[delta(('guest_cpu_ticks',key)) for key in cpu_keys]
    result['guest_steal_percent']=100*cpu_deltas[-1]/sum(cpu_deltas) if all(value is not None for value in cpu_deltas) and sum(cpu_deltas)>0 else None
    process_ranges={}
    for sample in samples:
        for row in sample.get('processes') or []:
            if row.get('present') is True and integer(row.get('pid')) and integer(row.get('start_ticks')):
                key=(row['pid'],row['start_ticks'])
                if key not in process_ranges:process_ranges[key]=[row,row,1]
                else:process_ranges[key][1]=row;process_ranges[key][2]+=1
    result['process_counter_deltas']=[]
    for (pid,start),(before,after,observations) in process_ranges.items():
        def difference(path):
            a,b=at(before,*path),at(after,*path)
            return b-a if observations>=2 and a is not None and b is not None and b>=a else None
        result['process_counter_deltas'].append({'pid':pid,'start_ticks':start,'observations':observations,'major_faults':difference(('major_faults',)),
            'user_ticks':difference(('cpu_ticks','user')),'system_ticks':difference(('cpu_ticks','system')),
            'read_bytes':difference(('io_bytes','read_bytes')),'write_bytes':difference(('io_bytes','write_bytes'))})
    result['cgroup_io_device_deltas']=[]
    if len(samples)>=2:
        first=at_dict(samples[0],'cgroup','io_stat')
        for device,row in at_dict(samples[-1],'cgroup','io_stat').items():
            before=first.get(device,{})
            result['cgroup_io_device_deltas'].append({'device':device,**{key:row[key]-before[key] if number(row.get(key)) and number(before.get(key)) and row[key]>=before[key] else None for key in ['rbytes','wbytes','rios','wios']}})
    directory_rows=[row for sample in samples for row in (sample.get('directories') or {}).get('paths',[]) if row.get('bounded_complete') is True]
    for key in ['bytes','segment_groups','merge_temporary_files']:
        values=[row[key] for row in directory_rows if number(row.get(key))]
        result['directory_'+key+'_peak']=max(values) if values else None
    return result

def percentile(values,quantile):
    return sorted(values)[max(0,math.ceil(len(values)*quantile)-1)] if values else None

def statistics(results):
    def summarize(rows):
        timings=[r['elapsed_ms'] for r in rows]
        return {'requests':len(rows),'p50_ms':percentile(timings,.5),'p95_ms':percentile(timings,.95),'p99_ms':percentile(timings,.99),
                'transport_failures':sum(r['failure'] not in {None,'wrong_ids'} for r in rows),'truth_mismatches':sum(r['failure']=='wrong_ids' for r in rows)}
    return {'all':summarize(results),'shared_native_features':summarize([r for r in results if r['shared']]),'new_features':summarize([r for r in results if not r['shared']])}

def phase(client,cases,truth,state,count,rate,detail,*,job_deadline=3,workers=4,queue_size=32,updates=None,arm=None):
    require(workers==4 and queue_size<=32 and count>0 and rate>0,'phase_bounds')
    jobs=queue.Queue(maxsize=queue_size);results=[];detail_errors=[];lock=threading.Lock();started=time.monotonic()
    def finish(case,due,failure,status=None):
        item={'case':case['id'],'shared':case['native_shared_features'],'elapsed_ms':max(0,(time.monotonic()-due)*1000),'failure':failure,'status':status}
        with lock:results.append(item)
        try:detail.write(item)
        except Exception:detail_errors.append(True)
    def worker():
        while True:
            job=jobs.get()
            try:
                if job is None:return
                case,due=job;minimum,_=state.get();status=None
                try:
                    if time.monotonic()>=due+job_deadline:raise TimeoutError('queue_deadline')
                    status,headers,value,_=client.request(query_path(case),deadline=due+job_deadline)
                    ack,pending=state.get();failure=check_answer(status,headers,value,case,truth,minimum,max(ack,pending or 0),arm)
                except Exception as error:failure='deadline' if isinstance(error,TimeoutError) else 'invalid_or_unavailable_response'
                finish(case,due,failure,status)
            finally:jobs.task_done()
    threads=[threading.Thread(target=worker,daemon=True) for _ in range(workers)]
    for thread in threads:thread.start()
    updater=None
    if updates:
        updater=threading.Thread(target=updates,args=(started,),daemon=True);updater.start()
    for i in range(count):
        due=started+i/rate;remaining=due-time.monotonic()
        if remaining>0:time.sleep(remaining)
        case=cases[i%len(cases)]
        try:jobs.put_nowait((case,due))
        except queue.Full:finish(case,due,'queue_full')
    for _ in threads:jobs.put(None)
    for thread in threads:thread.join(job_deadline*(queue_size//workers+2))
    require(not any(thread.is_alive() for thread in threads),'query_worker_not_drained')
    if updater:
        updater.join(32);require(not updater.is_alive(),'update_worker_not_drained')
    require(len(results)==count and not detail_errors,'request_recording_incomplete')
    return statistics(results)|{'wall_seconds':time.monotonic()-started,'scheduled_requests':count,'queue_deadline_seconds':job_deadline,'workers':workers,'queue_capacity':queue_size}

def updates_runner(control,state,records,errors,*,offset=15,interval=30):
    def run(started):
        try:
            for cursor in range(1,10):
                delay=started+offset+interval*(cursor-1)-time.monotonic()
                if delay>0:time.sleep(delay)
                state.begin(cursor);begin=time.monotonic();ack,_=control.ok('/apply/'+str(cursor),method='POST',seconds=15)
                require(ack.get('acked')==cursor and ack.get('replay') is False,'batch_ack_state');state.commit(cursor)
                replay,_=control.ok('/apply/'+str(cursor),method='POST',seconds=15)
                require(replay.get('acked')==cursor and replay.get('replay') is True,'batch_replay_state')
                records.append({'cursor':cursor,'seconds':time.monotonic()-begin,'replay_confirmed':True})
        except Exception as error:
            code=str(error) if isinstance(error,(QualificationError,ObservationError)) and re.fullmatch('[A-Za-z0-9_]{1,100}',str(error)) else type(error).__name__
            errors.append(code)
    return run

def verify_identity(search,control,arm,prepared,nonce,binary,fixture=False,expected_cursor=None):
    current,_=control.ok('/prepared',seconds=3)
    for key in ['machine_id','volume_id','guest','final_volume_gib','public_services','observer_receipt_sha256','corpus_sha256','updates_sha256','build_id','source_watermark','actor_count']:require(current.get(key)==prepared.get(key),'phase_prepared_identity_'+key)
    value,_=search.ok('/__evaluation',seconds=3)
    if value.get('ready') is False:raise NotReady()
    require(value.get('ready') is True and value.get('source_ready_attestation') is False,'evaluation_boolean_types')
    for key,target in [('arm',arm),('nonce',nonce),('machine_id',prepared['machine_id']),('volume_id',prepared['volume_id']),('guest',SHAPE),('corpus_sha256',prepared['corpus_sha256']),('updates_sha256',prepared['updates_sha256']),('observer_receipt_sha256',prepared['observer_receipt_sha256']),('engine_binary_sha256',binary),('source_ready_attestation',False),('source_kind','frozen-release-snapshot'),('ready',True)]:require(value.get(key)==target,'evaluation_'+key)
    require(value.get('runtime_process_verified') is True and value.get('process_executable_sha256')==binary or fixture and value.get('synthetic_local_fixture') is True,'runtime_binary_not_verified')
    if arm=='tantivy':require(value.get('engine_settings')=={'retrieval_strategy':'global-merge','writer_memory_mb':128,'concurrency':4,'max_candidates':10000},'engine_configuration')
    if expected_cursor is not None:require(current.get('acked')==expected_cursor,'phase_cursor_state')
    identity={key:value.get(key) for key in ['engine_binary_sha256','observer_receipt_sha256','runtime_process_verified','engine_settings']}
    if not fixture:identity['provider']=verify_provider(arm,prepared,value)
    return identity

def wait_identity(*args,until,**kwargs):
    while time.monotonic()<until:
        try:return verify_identity(*args,**kwargs)
        except (OSError,ValueError,ObservationError,NotReady,http.client.HTTPException):time.sleep(.2)
    raise QualificationError('engine_start_deadline')

def exercise(arm,search,control,prepared,cases,truth,nonce,binary,root,deadline,fixture=False,phases=PHASES,update_offset=15,update_interval=30):
    state=State();receipt={'arm':arm,'phases':{},'updates':[]};detail=Detail(root/(arm+'-requests.jsonl'));metrics=Metrics(control,root/(arm+'-resources.jsonl'))
    try:
        control.ok('/serve',method='POST');serve_started=time.monotonic()
        wait_identity(search,control,arm,prepared,nonce,binary,fixture,expected_cursor=0,until=time.monotonic()+20)
        metrics.start();update_errors=[]
        for name,count,rate in phases:
            require(time.time()+count/rate+40<deadline and time.monotonic()-serve_started+count/rate+40<900,'arm_measurement_budget')
            receipt['identity']=wait_identity(search,control,arm,prepared,nonce,binary,fixture,expected_cursor=state.get()[0],until=time.monotonic()+20)
            update=updates_runner(control,state,receipt['updates'],update_errors,offset=update_offset,interval=update_interval) if name=='mixed' else None
            resource_start=len(metrics.samples);metrics.sample()
            receipt['phases'][name]=phase(search,cases,truth,state,count,rate,detail,updates=update,arm=None if fixture else arm)
            metrics.sample();receipt['phases'][name]['resources']=summarize_metrics(metrics.samples[resource_start:])
            require(not update_errors,'update_protocol_failed_'+(update_errors[0] if update_errors else 'unknown'))
            if name=='mixed':require(state.get()==(9,None) and len(receipt['updates'])==9,'feed_not_drained')
        require(state.get()==(9,None),'final_cursor_not_nine')
        control.ok('/restart',method='POST');receipt['restart_identity']=wait_identity(search,control,arm,prepared,nonce,binary,fixture,expected_cursor=9,until=time.monotonic()+20)
        replay,_=control.ok('/apply/9',method='POST');require(replay.get('acked')==9 and replay.get('replay') is True,'durable_replay_not_current')
        receipt['post_restart_quiescent']=phase(search,cases,truth,state,len(cases),10 if not fixture else 1000,detail,arm=None if fixture else arm)
        receipt['post_restart_quiescent']['performance_phase']=False
        require(time.monotonic()-serve_started<900,'arm_measurement_window_exceeded')
        receipt['durable_cursor_replay_confirmed']=True
    finally:
        try:receipt['resources']=metrics.close()
        finally:
            detail.close();control.ok('/pause',method='POST')
    return receipt

def gates(arms):
    native,tantivy=arms['native'],arms['tantivy'];relative={};all_truth=True;absolute=True;shared_transport=True
    for name,_,_ in PHASES:
        a=native['phases'][name];b=tantivy['phases'][name]
        comparisons={}
        for p in ['p95_ms','p99_ms']:
            x,y=a['shared_native_features'][p],b['shared_native_features'][p]
            comparisons[p]=x is not None and y is not None and y<=x
        comparisons['passed']=all(comparisons.values());relative[name]=comparisons
        shared_transport &= a['shared_native_features']['transport_failures']==0
        all_truth &= b['all']['transport_failures']==0 and b['all']['truth_mismatches']==0
        if name in {'warm','mixed'}:
            absolute &= b['all']['p95_ms'] is not None and b['all']['p95_ms']<=50 and b['all']['p99_ms']<=100
            if b['new_features']['requests']:
                absolute &= b['new_features']['p95_ms']<=50 and b['new_features']['p99_ms']<=100
    shared_transport &= native['post_restart_quiescent']['shared_native_features']['transport_failures']==0
    post=tantivy['post_restart_quiescent']['all'];all_truth &= post['transport_failures']==0 and post['truth_mismatches']==0
    resources={}
    for arm,receipt in arms.items():
        value=receipt.get('resources',{})
        complete=value.get('samples',0)>=2 and value.get('sample_failures')==0 and all(number(value.get(key)) for key in ['guest_available_min_bytes','guest_major_faults_delta','free_disk_min_bytes','oom_kill_delta','swap_in_pages_delta','swap_out_pages_delta','maximum_sample_gap_seconds'])
        resources[arm]=complete and value['oom_kill_delta']==0 and value['swap_in_pages_delta']==0 and value['swap_out_pages_delta']==0 and value['free_disk_min_bytes']>=1024**3 and value['maximum_sample_gap_seconds']<=8
    performance_passed=shared_transport and all_truth and absolute and all(row['passed'] for row in relative.values()) and all(resources.values())
    observation={arm:receipt.get('resources',{}).get('memory_io_observation',{}).get('qualified') is True for arm,receipt in arms.items()}
    passed=performance_passed and all(observation.values())
    return {'passed':passed,'performance_component_passed':performance_passed,'memory_io_observation_qualified':observation,'comparison_status':'conclusive' if shared_transport else 'inconclusive_native_shared_transport_failures','tantivy_truth_and_transport_passed':all_truth,'tantivy_warm_mixed_absolute_slo_passed':absolute,'relative_every_phase':relative,'resource_observation_and_no_oom_swap':resources}

def main():
    deadline=float(os.environ['QUAL_DEADLINE']);require(time.time()<deadline<=time.time()+5400,'generator_lease')
    def expired(*_):raise QualificationError('generator_hard_deadline')
    signal.signal(signal.SIGALRM,expired);signal.alarm(max(1,int(deadline-time.time())))
    require(os.environ.get('FLY_APP_NAME')==APP,'generator_app')
    nonce=os.environ['QUAL_PILOT_NONCE'];secret=os.environ['QUAL_CONTROL_SECRET'];require(16<=len(nonce)<=128 and bool(secret),'generator_capabilities')
    searches={arm:Client(os.environ['QUAL_'+arm.upper()+'_URL'],8080) for arm in ['native','tantivy']}
    controls={arm:Client(os.environ['QUAL_'+arm.upper()+'_CONTROL_URL'],8082,secret) for arm in searches}
    require(all(searches[arm].machine==controls[arm].machine for arm in searches) and searches['native'].machine!=searches['tantivy'].machine,'owned_control_association')
    binaries={arm:os.environ['QUAL_'+arm.upper()+'_BINARY_SHA256'] for arm in searches};require(all(sha(value) for value in binaries.values()),'frozen_binary_hashes')
    root=Path('/tmp/qualification-full-generator');root.mkdir(mode=0o700);(root/'owner').write_text('full-generator-v1')
    require(time.time()+1200<deadline,'insufficient_comparison_lease')
    observed,observation_failures=wait_prepared(controls,deadline-1200,nonce,provider=lambda machine:provider_state(machine,os.environ.get('QUAL_REAPER_CAPABILITY')))
    validate_prepared(observed,nonce)
    require(all(observed[arm]['machine_id']==controls[arm].machine for arm in controls),'prepared_owned_machine')
    cases,truth,oracle_proof=load_inputs(controls['native'],observed['native'],os.environ['QUAL_ORACLE_BINARY_SHA256'],root)
    arms={}
    for arm in ['native','tantivy']:
        arms[arm]=exercise(arm,searches[arm],controls[arm],observed[arm],cases,truth,nonce,binaries[arm],root,deadline)
    result={'schema_version':1,'scope':'full-corpus-performance-component/frozen-feed','passed':False,'full_qualification':False,'live_policy_source_qualified':False,'production_contact':False,
        'gates':gates(arms),'case_count':len(cases),'shared_case_count':sum(case['native_shared_features'] for case in cases),'oracle':oracle_proof,
        'input_hashes':{key:observed['native'][key] for key in ['corpus_sha256','updates_sha256','cases_sha256','oracle_sha256']},'preparation_observation_failures':observation_failures,
        'arms':arms,'raw_detail_location':'cloud-local ephemeral generator /tmp/qualification-full-generator'}
    result['passed']=result['gates']['passed'];raw=encoded(result);require(len(raw)<=128*1024,'receipt_byte_budget')
    (root/'receipt.json').write_bytes(raw+b'\n')
    log=result if len(raw)<=64*1024 else {key:value for key,value in result.items() if key!='arms'}|{'metadata_file':str(root/'receipt.json'),'metadata_sha256':digest(raw),'metadata_bytes':len(raw)}
    lograw=encoded(log);require(len(lograw)<=64*1024,'log_receipt_byte_budget');print('QUALIFICATION_FULL_RESULT '+lograw.decode(),flush=True)
    if not result['passed']:raise SystemExit(2)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('QUALIFICATION_FULL_RESULT '+json.dumps({'passed':False,'full_qualification':False,'production_contact':False,'status':'resource_inconclusive' if isinstance(error,QualificationError) and str(error).startswith('input_resource_cap_') else 'failed','error_class':type(error).__name__,'error_code':str(error) if isinstance(error,QualificationError) else 'generator_failed'},separators=(',',':')),flush=True)
        raise SystemExit(1)

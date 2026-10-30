import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request
import urllib.error

APP = 'typeahead-qual-20261029'
OWNER = 'tantivy-qualification-20261029'
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT/'resources.json'
PRIVATE = Path('/tmp/typeahead-qual-20261029.control.private.json')
PROTECTED = {'8e3d79b70dd748', '801539a6551778', '7845636a3d0ee8', '840e95a2153308'}
LAST_API_FAILURE = None


def ledger():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {'schema': 1, 'app': APP, 'owner': OWNER, 'machines': [], 'volumes': []}


def save(value):
    assert value['app'] == APP and value['owner'] == OWNER
    part = LEDGER.with_suffix('.tmp')
    part.write_text(json.dumps(value, indent=2)+'\n')
    part.replace(LEDGER)


def credential():
    if PRIVATE.exists():
        if PRIVATE.stat().st_mode & 0o077:
            raise RuntimeError('private control credential permissions differ')
        value = json.loads(PRIVATE.read_text())
        if value['app'] != APP or value['expires_at'] <= time.time()+60:
            raise RuntimeError('qualification capability expired; explicitly reconcile and renew')
        return value['token']
    result = subprocess.run(['fly', 'tokens', 'create', 'deploy', '-a', APP, '--expiry', '4h', '--name', OWNER+'-'+secrets.token_hex(4), '--json'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('qualification app capability creation failed')
    raw = json.loads(result.stdout)
    value = {'app': APP, 'token': raw['token'] if isinstance(raw, dict) else raw, 'expires_at': time.time()+4*3600}
    with open(PRIVATE, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        json.dump(value, file)
    return value['token']


def api(method, path, body=None):
    global LAST_API_FAILURE
    if path.startswith('/') or '..' in path:
        raise ValueError('invalid qualification API path')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError('qualification API redirect rejected')
    request = urllib.request.Request('https://api.machines.dev/v1/apps/'+APP+'/'+path,
        method=method, data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+credential(),'Content-Type':'application/json'})
    timeout = max(15,body.get('timeout',0)+10) if isinstance(body,dict) and path.endswith('/exec') else 15
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=timeout) as response:
            raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise RuntimeError('qualification API response budget')
        return json.loads(raw) if raw and method!='DELETE' else None
    except urllib.error.HTTPError as error:
        code=error.code;error.close()
        LAST_API_FAILURE={'method':method,'path':path,'error_class':'HTTPError','status':code,'observed_at':time.time()}
        raise RuntimeError('qualification API HTTP '+str(code)) from None
    except (OSError,ValueError) as error:
        LAST_API_FAILURE={'method':method,'path':path,'error_class':type(error).__name__,'status':None,'observed_at':time.time()}
        raise RuntimeError('qualification API transport failed') from None


def owned(machine_id):
    if machine_id in PROTECTED or machine_id not in [m['id'] for m in ledger()['machines']]:
        raise RuntimeError('machine is outside the recorded qualification owner')
    machine = api('GET', 'machines/'+machine_id)
    if machine['config'].get('metadata',{}).get('owner') != OWNER:
        raise RuntimeError('qualification machine owner differs')
    return machine


def attest(machine_id, destination):
    machine = owned(machine_id)
    config = machine['config']
    volumes = {v['id']:v for v in api('GET','volumes')}
    mounts = [{'volume_id':m['volume'],'path':m['path'],'size_gib':volumes[m['volume']]['size_gb'],'encrypted':volumes[m['volume']]['encrypted']} for m in config.get('mounts',[])]
    value = {'schema':1,'provider':'fly','observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'app':APP,'machine_id':machine['id'],'instance_id':machine['instance_id'],'state':machine['state'],'image_digest':config['image'],'guest':config['guest'],'mounts':mounts,'owner':config['metadata']['owner'],'role':config['metadata']['role'],'public_service_ports':[p['port'] for s in config.get('services',[]) for p in s.get('ports',[])],'provider_config_reserved_env_overrides':any(k in config.get('env',{}) for k in ['FLY_MACHINE_ID','FLY_MACHINE_VERSION'])}
    if value['public_service_ports'] or value['provider_config_reserved_env_overrides']:
        raise RuntimeError('qualification isolation differs')
    value.update(volume_ids=[m['volume_id'] for m in mounts],final_volume_gib=sum(m['size_gib'] for m in mounts),public_services=config.get('services') or [],machines_api_response_sha256=hashlib.sha256(json.dumps(machine,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    Path(destination).write_text(json.dumps(value,indent=2)+'\n')
    return value


def log_receipts(machine_id):
    owned(machine_id)
    result = subprocess.run(['fly', 'logs', '-a', APP, '-m', machine_id, '--json', '--no-tail'],
                            env={**os.environ, 'FLY_API_TOKEN': credential()}, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError('qualification log read failed')
    data = result.stdout
    decoder = json.JSONDecoder()
    receipts = []
    markers = ['QUALIFICATION_REAPER_ARMED ', 'QUALIFICATION_REAPER_RECEIPT ',
               'QUALIFICATION_REAPER_RESOLVED ', 'QUALIFICATION_PILOT_RESULT ', 'QUALIFICATION_FULL_RESULT ', 'QUALIFICATION_PREPARATION_COMPLETE ', 'QUALIFICATION_SYNTHETIC_STAGED_PILOT_RESULT ', 'PILOT_PREPARED ']
    while data.strip():
        item, consumed = decoder.raw_decode(data.lstrip())
        data = data.lstrip()[consumed:]
        message = item.get('message', '')
        for marker in markers:
            if message.startswith(marker):
                receipts.append({'marker': marker.strip(), 'receipt': json.loads(message[len(marker):])})
                break
    return receipts

FUNCTIONAL_SCRIPT_SHA256 = 'caa7cd176ce912d8561f9ddc7c1e351a710169b42e814db663d32ce0be6ca615'


def functional_script_sha256():
    source = (ROOT/'image/full_functional.py').read_bytes()
    if len(source) > 32768 or hashlib.sha256(source).hexdigest() != FUNCTIONAL_SCRIPT_SHA256:
        raise RuntimeError('functional script differs from committed binding')
    return FUNCTIONAL_SCRIPT_SHA256


def _execute_functional(machine_id, source, arguments, expected_sha256, transport, target='/tmp/full_functional.py'):
    import base64
    if len(source) > 32768 or hashlib.sha256(source).hexdigest() != expected_sha256:
        raise RuntimeError('functional upload hash differs')
    def execute(code, timeout=15, exit_codes=(0,)):
        response = transport('POST', 'machines/'+machine_id+'/exec', {'command':['python3','-c',code], 'timeout':timeout})
        if not isinstance(response,dict) or response.get('exit_code') not in exit_codes or response.get('exit_signal',0) != 0:
            raise RuntimeError('functional execution unconfirmed')
        if not isinstance(response.get('stdout',''),str) or len(response.get('stdout','').encode()) > 65536:
            raise RuntimeError('functional output budget')
        return response.get('stdout','')
    for offset in range(0,len(source),1800):
        chunk = base64.b64encode(source[offset:offset+1800]).decode()
        flags = 'os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW' if offset == 0 else 'os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW'
        code = ('import os,base64;fd=os.open('+repr(target)+','+flags+',0o600);'
                'assert os.fstat(fd).st_size=='+str(offset)+';'
                'data=base64.b64decode('+repr(chunk)+');assert os.write(fd,data)==len(data);os.fsync(fd);os.close(fd)')
        execute(code)
    code = ('import hashlib,subprocess,sys;path='+repr(target)+';'
            'assert hashlib.sha256(open(path,"rb").read()).hexdigest()=='+repr(expected_sha256)+';'
            'result=subprocess.run([sys.executable,path]+('+repr(arguments)+'),timeout=48,capture_output=True,text=True);'
            'assert len(result.stdout.encode())<=65536;sys.stdout.write(result.stdout);sys.exit(result.returncode)')
    output = execute(code,50,(0,1,2))
    marker = 'QUALIFICATION_POST_MEASUREMENT_FUNCTIONAL '
    rows = [json.loads(line[len(marker):]) for line in output.splitlines() if line.startswith(marker)]
    if len(rows) != 1 or not isinstance(rows[0],dict):
        raise RuntimeError('functional receipt missing')
    return rows[0]


def run_functional(tantivy_id, native_id, run_id, pilot, timed_result_sha256):
    import re
    functional_script_sha256()
    current = ledger().get('pilot',{})
    if current.get('manifest',{}).get('run_id') != run_id or current.get('resolved') != pilot.get('resolved'):
        raise RuntimeError('functional run differs')
    if current.get('manifest_sha256') != pilot.get('manifest_sha256') or time.time()+90 >= current['manifest']['deadline']:
        raise RuntimeError('functional lease differs')
    if current['resolved'].get('machine:tantivy-control') != tantivy_id or current['resolved'].get('machine:native-control') != native_id:
        raise RuntimeError('functional resource binding differs')
    intent = current.get('supervisor',{}).get('functional',{})
    if intent.get('state') != 'attempted' or intent.get('timed_result_sha256') != timed_result_sha256 or not re.fullmatch('[a-f0-9]{64}',timed_result_sha256):
        raise RuntimeError('functional writeahead missing')
    for role, mid in [('tantivy-control',tantivy_id),('native-control',native_id)]:
        actual = owned(mid)
        metadata = actual.get('config',{}).get('metadata',{})
        if actual.get('state') != 'started' or metadata.get('run_id') != run_id or metadata.get('role') != role or actual.get('config',{}).get('env',{}).get('QUAL_PILOT_NONCE') != current['nonce']:
            raise RuntimeError('functional engine differs')
    def origin(mid, port):return 'http://'+mid+'.vm.'+APP+'.internal:'+str(port)
    arguments = ['--native-control-url',origin(native_id,8082),'--tantivy-control-url',origin(tantivy_id,8082),
                 '--tantivy-url',origin(tantivy_id,8080),'--oracle-binary-sha256',current['oracle_binary_sha256'],
                 '--tantivy-binary-sha256',current['tantivy_binary_sha256'],'--timed-result-sha256',timed_result_sha256]
    receipt = _execute_functional(tantivy_id,(ROOT/'image/full_functional.py').read_bytes(),arguments,FUNCTIONAL_SCRIPT_SHA256,api)
    if receipt.get('timed_result_sha256') != timed_result_sha256:
        raise RuntimeError('functional result binding differs')
    return receipt

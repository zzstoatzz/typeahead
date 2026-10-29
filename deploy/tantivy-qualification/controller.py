import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

APP = 'typeahead-qual-20261029'
OWNER = 'tantivy-qualification-20261029'
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT/'resources.json'
PRIVATE = Path('/tmp/typeahead-qual-20261029.control.private.json')
PROTECTED = {'8e3d79b70dd748', '801539a6551778', '7845636a3d0ee8', '840e95a2153308'}


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
    if path.startswith('/') or '..' in path:
        raise ValueError('invalid qualification API path')
    source = "let s='';for await(const b of process.stdin)s+=b;const p=JSON.parse(s);const r=await fetch(p.url,{method:p.method,headers:{Authorization:'Bearer '+p.token,'Content-Type':'application/json'},...(p.body===null?{}:{body:JSON.stringify(p.body)}),signal:AbortSignal.timeout(60000)});console.log(JSON.stringify({status:r.status,body:await r.text()}));"
    result = subprocess.run(['node', '--input-type=module', '-e', source], input=json.dumps({'url':'https://api.machines.dev/v1/apps/'+APP+'/'+path,'method':method,'body':body,'token':credential()}), capture_output=True,text=True,timeout=65)
    if result.returncode:
        raise RuntimeError('qualification API transport failed')
    response = json.loads(result.stdout)
    if response['status'] >= 400:
        raise RuntimeError('qualification API HTTP '+str(response['status']))
    return json.loads(response['body']) if response['body'] and method != 'DELETE' else None


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
               'QUALIFICATION_REAPER_RESOLVED ', 'QUALIFICATION_PILOT_RESULT ', 'QUALIFICATION_FULL_RESULT ', 'PILOT_PREPARED ']
    while data.strip():
        item, consumed = decoder.raw_decode(data.lstrip())
        data = data.lstrip()[consumed:]
        message = item.get('message', '')
        for marker in markers:
            if message.startswith(marker):
                receipts.append({'marker': marker.strip(), 'receipt': json.loads(message[len(marker):])})
                break
    return receipts

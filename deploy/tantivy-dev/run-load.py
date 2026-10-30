import base64,hashlib,json,os,subprocess,sys,time,urllib.request,zlib
from pathlib import Path
import control

ROOT=Path(__file__).resolve().parent
RECEIPT=ROOT/'load-machine.json'
MARKER='TANTIVY_DEV_LOAD_RESULT '

def start():
 d=control.ledger();primary=control.owned(d['machine_id'])
 if d['phase']!='publish' or primary['config']['guest']!={'cpu_kind':'shared','cpus':2,'memory_mb':1024}:
  raise RuntimeError('load requires published final serving shape')
 with urllib.request.urlopen('https://'+control.APP+'.fly.dev/ready',timeout=10) as response:
  if not json.load(response).get('ready'):raise RuntimeError('dev service not ready')
 for machine in control.api('GET','machines'):
  if machine['config'].get('metadata',{}).get('role')=='load-generator':raise RuntimeError('existing load generator must be reconciled')
 source=(ROOT/'load.py').read_bytes();compressed=zlib.compress(source,9)
 code=base64.b64encode(compressed).decode()
 command="import base64,signal,zlib;signal.alarm(900);exec(compile(zlib.decompress(base64.b64decode('"+code+"')),'<cloud-load>','exec'))"
 machine=control.api('POST','machines',{'name':'tantivy-dev-load-'+str(int(time.time())),'region':'ewr','skip_service_registration':True,'config':{'image':d['image'],'auto_destroy':True,'guest':{'cpu_kind':'shared','cpus':1,'memory_mb':256},'services':[],'restart':{'policy':'no'},'env':{'TANTIVY_LOAD_TARGET':'https://'+control.APP+'.fly.dev'},'init':{'exec':['python3','-c',command]},'metadata':{'owner':d['owner'],'role':'load-generator'}}})
 receipt={'machine_id':machine['id'],'serving_machine_id':d['machine_id'],'app':control.APP,'created_at':time.time(),'deadline_seconds':900,'auto_destroy':True,'image':d['image'],'guest':machine['config']['guest'],'load_source_sha256':hashlib.sha256(source).hexdigest(),'source_bytes':len(source),'compressed_bytes':len(compressed)}
 RECEIPT.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))

def collect():
 d=json.loads(RECEIPT.read_text())
 logs=subprocess.check_output(['fly','logs','-a',control.APP,'--instance',d['machine_id'],'--no-tail'],env={**os.environ,'FLY_API_TOKEN':control.credential()},stdin=subprocess.DEVNULL,text=True,timeout=30)
 selected=[];result=None;phases={}
 for line in logs.splitlines():
  for marker in ['TANTIVY_DEV_LOAD_PHASE ',MARKER]:
   if marker in line:
    raw=line.split(marker,1)[1];value=json.loads(raw);selected.append(marker+json.dumps(value,separators=(',',':')))
    if marker==MARKER:result=value
    else:phases[value['phase']]=value
 (ROOT/'load-results.log').write_text('\n'.join(selected)+'\n')
 if result is not None:
  if set(phases)!={p['phase'] for p in result['phases']}:raise RuntimeError('phase evidence missing; inspect same job logs')
  result['phases']=[phases[p['phase']] for p in result['phases']]
  (ROOT/'load-results.json').write_text(json.dumps(result,indent=2)+'\n')
 machines=control.api('GET','machines');remaining=[m for m in machines if m['id']==d['machine_id']]
 print(json.dumps({'result_captured':result is not None,'load_machine_present':bool(remaining),'passed_correctness':None if result is None else result['passed_correctness'],'latency_gate':None if result is None else result['latency_gate']}))
 if result is None:raise RuntimeError('load result not yet captured; inspect same job, do not start another')

if __name__=='__main__':
 if sys.argv[1]=='start':start()
 elif sys.argv[1]=='collect':collect()
 else:raise ValueError('expected start or collect')

import base64,hashlib,json,os,subprocess
from pathlib import Path
import control

ROOT=Path(__file__).resolve().parent
MACHINE='840e95a2153308'
def remote(source):
 encoded=base64.b64encode(source.encode()).decode()
 command="python3 -c 'import base64;exec(base64.b64decode(\""+encoded+"\"))'"
 result=subprocess.run(['fly','machine','exec',MACHINE,'-a','typeahead-tantivy-dev',command],env={**os.environ,'FLY_API_TOKEN':control.credential()},stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)
 if result.returncode or result.stderr or 'HELPER_OK' not in result.stdout:
  raise RuntimeError('cloud helper staging failed; no success marker')
 return result.stdout

def stage():
 receipt={}
 for filename in ['qualify-real.py','sample-resources.py','summarize-resources.py']:
  data=(ROOT/filename).read_bytes();target='/tmp/tantivy-dev-'+filename
  for offset in range(0,len(data),2048):
   encoded=base64.b64encode(data[offset:offset+2048]).decode()
   source='import os,base64;from pathlib import Path;assert os.environ.get("FLY_APP_NAME")=="typeahead-tantivy-dev";assert os.environ.get("FLY_MACHINE_ID")=="'+MACHINE+'";'
   source+='p=Path('+repr(target)+');'
   source+='p.open('+repr('wb' if offset==0 else 'ab')+').write(base64.b64decode('+repr(encoded)+'));print("HELPER_OK")'
   remote(source)
  digest=hashlib.sha256(data).hexdigest()
  remote('import hashlib;from pathlib import Path;assert hashlib.sha256(Path('+repr(target)+').read_bytes()).hexdigest()=='+repr(digest)+';print("HELPER_OK")')
  receipt[target]={'sha256':digest,'bytes':len(data),'cloud_verified':True,'machine_id':MACHINE}
 (ROOT.parents[1]/'docs/evals/tantivy-dev-20261028/cloud-helper-files.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({'cloud_helpers_verified':len(receipt)}))

if __name__=="__main__":stage()

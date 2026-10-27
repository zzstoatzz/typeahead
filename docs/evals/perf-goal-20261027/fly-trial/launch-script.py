import hashlib,json,os,shlex,subprocess,uuid
from pathlib import Path
run='20261027-'+uuid.uuid4().hex[:12]
record=json.loads(Path('/tmp/typeahead-perf-fly-image.json').read_text())
image=record['digests'][0]
unit='typeahead-perf-fly-'+run
remote='/var/lib/typeahead-perf-code/'+run
output='/var/lib/typeahead-perf-fly/'+run
credential='/run/'+unit+'.token'
command='/usr/bin/python3 '+remote+'/fly_manager.py --run '+run+' --image '+image+' --output '+output
unit_text='\n'.join(['[Unit]','Description=Bounded private Typeahead performance qualification','[Service]','Type=exec','User=root','StateDirectory=typeahead-perf-fly/'+run,'LoadCredential=fly-token:'+credential,'ExecStart='+command,'ExecStopPost='+command+' --cleanup-only','RuntimeMaxSec=5400','TimeoutStopSec=180','KillMode=control-group','CPUQuota=10%','MemoryMax=128M','TasksMax=16','NoNewPrivileges=yes','ProtectSystem=strict','ProtectHome=yes','PrivateTmp=yes','CapabilityBoundingSet=','RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX','UMask=0077',''])
payload=json.dumps({'root':remote,'code':Path('scripts/perf/fly_manager.py').read_text(),'unit':unit,'unit_text':unit_text}).encode()
setup="import json,sys,pathlib,subprocess; p=json.load(sys.stdin); root=pathlib.Path(p['root']); root.mkdir(parents=True,exist_ok=False); (root/'fly_manager.py').write_text(p['code']); (root/'fly_manager.py').chmod(0o644); pathlib.Path('/etc/systemd/system/'+p['unit']+'.service').write_text(p['unit_text']); subprocess.run(['systemctl','daemon-reload'],check=True)"
subprocess.run(['ssh','-o','BatchMode=yes','root@100.78.52.120','python3 -c '+shlex.quote(setup)],input=payload,check=True)
result=subprocess.run(['fly','tokens','create','deploy','-a','typeahead-startup-20261024','--expiry','2h','--name',unit,'--json'],capture_output=True,check=True)
parsed=json.loads(result.stdout)
token=parsed.get('token') if isinstance(parsed,dict) else parsed
if not isinstance(token,str) or not token or '\n' in token: raise RuntimeError('unexpected token structure; no credential printed')
launch="import os,sys,subprocess,pathlib; path=pathlib.Path("+repr(credential)+"); fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,sys.stdin.buffer.read()); os.close(fd); result=subprocess.run(['systemctl','start',"+repr(unit)+"]); path.unlink(); sys.exit(result.returncode)"
subprocess.run(['ssh','-o','BatchMode=yes','root@100.78.52.120','python3 -c '+shlex.quote(launch)],input=token.encode(),check=True)
record.update(run_id=run,unit=unit,remote_evidence=output,token_expiry='2h from launch',manager_sha256=hashlib.sha256(Path('scripts/perf/fly_manager.py').read_bytes()).hexdigest())
Path('docs/evals/perf-goal-20261027/fly-trial/launch-'+run+'.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))

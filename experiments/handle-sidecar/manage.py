import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

APP='typeahead-search-refresh'
VOLUME='vol_re1ql0132o3qxw34'
NAME='handle-sidecar-20261028'
OUT=Path(sys.argv[1]).resolve()


def cli(*args):
    return subprocess.check_output(['fly',*args],stderr=subprocess.PIPE,text=True,timeout=90)


def owned():
    machines=json.loads(cli('machine','list','-a',APP,'--json'))
    candidates=[m for m in machines if m['name']==NAME]
    if len(candidates)!=1:raise RuntimeError('owned target identity missing/ambiguous')
    m=candidates[0];c=m['config']
    assert c['metadata']['owner']==NAME and not c.get('services')
    assert c['guest']['cpus']==2 and c['guest']['memory_mb']==1024
    assert [x['volume'] for x in c['mounts']]==[VOLUME]
    return m


def execute(mid,cmd):
    r=json.loads(cli('machine','exec',mid,cmd,'-a',APP,'--json','--timeout','30'))
    if r.get('exit_code',0):raise RuntimeError('private command failed')
    return r['stdout']


def main():
    m=owned();mid=m['id'];(OUT/'owned.json').write_text(json.dumps({'machine':mid,'volume':VOLUME,'config':m['config']},indent=2)+'\n')
    deadline=time.monotonic()+5100
    try:
        while time.monotonic()<deadline:
            try:
                status=json.loads(execute(mid,"python3 -c 'from pathlib import Path; p=Path(\"/data/research-evidence/status.json\"); print(p.read_text() if p.exists() else \"{}\")'"))
            except (subprocess.SubprocessError,RuntimeError,ValueError):
                time.sleep(10);continue
            (OUT/'status.json').write_text(json.dumps(status,indent=2)+'\n')
            if status.get('phase')=='ready':
                size=status['bytes'];assert 0<size<16*1024*1024
                data=bytearray()
                while len(data)<size:
                    command=f"python3 -c 'import base64; f=open(\"/data/research-evidence/results.tgz\",\"rb\"); f.seek({len(data)}); print(base64.b64encode(f.read(24576)).decode())'"
                    data.extend(base64.b64decode(execute(mid,command).strip(),validate=True))
                assert len(data)==size and hashlib.sha256(data).hexdigest()==status['sha256']
                (OUT/'results.tgz').write_bytes(data)
                break
            time.sleep(20)
        else:raise TimeoutError('sidecar trial deadline')
    finally:
        owned()
        cli('machine','destroy',mid,'-a',APP,'--force')
        remaining=json.loads(cli('machine','list','-a',APP,'--json'))
        assert all(x['id']!=mid for x in remaining)
        volumes=json.loads(cli('volumes','list','-a',APP,'--json'))
        v=next(x for x in volumes if x['id']==VOLUME)
        assert v['name']=='fst_research_20261028' and not v.get('attached_machine_id')
        cli('volumes','destroy',VOLUME,'-a',APP,'--yes')
        for _ in range(30):
            volumes=json.loads(cli('volumes','list','-a',APP,'--json'))
            v=next((x for x in volumes if x['id']==VOLUME),None)
            if not v:break
            time.sleep(2)
        (OUT/'cleanup.json').write_text(json.dumps({'machine_absent':True,'volume':v,'at_unix':time.time()},indent=2)+'\n')


if __name__=='__main__':main()

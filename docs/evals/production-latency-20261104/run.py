import concurrent.futures
import json
import pathlib
import signal
import subprocess
import time
import urllib.parse

root = pathlib.Path('/tmp/typeahead-prod-verify-20261104')
app = 'typeahead-eu-probe-' + str(int(time.time()))
queries = ['a', 'b', 'jola.dev', 'nate', 'jo', 'sky', 'sarah', 'André', 'nate kohari', 'zzstoatzz.io']
manifest = {'owner': 'Codex for Nate: production latency verification', 'app': app, 'started_at': time.time(), 'scratch_budget_bytes': 20971520, 'regions': ['lhr','arn'], 'volumes': False, 'public_services': False, 'machines': [], 'requests': []}
config = ['silent', 'show-error', 'http2', 'connect-timeout = 3', 'max-time = 8', 'rate = "1/s"', 'user-agent = "typeahead-prod-verification/1.0"', 'header = "X-Client: bench"', 'write-out = "{\\"curl\\":%{json},\\"headers\\":%{header_json}}\\n"']
for phase in range(3):
    for q in queries:
        for side in (['ours','bluesky'] if phase % 2 == 0 else ['bluesky','ours']):
            host = 'typeahead.waow.tech' if side == 'ours' else 'public.api.bsky.app'
            url = 'https://' + host + '/xrpc/app.bsky.actor.searchActorsTypeahead?' + urllib.parse.urlencode({'q':q, 'limit':14})
            manifest['requests'].append({'phase':phase,'query':q,'side':side,'url':url})
            config.extend(['url = ' + json.dumps(url), 'output = "/dev/null"'])
(root/'probe.conf').write_text('\n'.join(config)+'\n')
def save():
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
def run(args,timeout=45):
    p = subprocess.run(['fly',*args],capture_output=True,text=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError(str(args[:3])+': '+p.stderr[-1000:]+p.stdout[-1000:])
    return p.stdout
def interrupt(*_):
    raise KeyboardInterrupt()
signal.signal(signal.SIGTERM,interrupt)
owned=False
save()
try:
    run(['apps','create',app,'--org','personal','--yes','--json'])
    owned=True
    for region in manifest['regions']:
        result=run(['machine','run','curlimages/curl:8.17.0','--app',app,'--region',region,'--vm-size','shared-cpu-1x','--vm-memory','256','--restart','no','--name','probe-'+region,'--entrypoint','/bin/sh','--file-local','/tmp/probe.conf='+str(root/'probe.conf'),'--detach','--','-c','sleep 600'],timeout=90)
        (root/(region+'-create.txt')).write_text(result)
        machines=json.loads(run(['machine','list','--app',app,'--json']))
        machine=next(m for m in machines if m['name']=='probe-'+region)
        manifest['machines'].append({'id':machine['id'],'region':machine['region']})
        save()
        print('Ready '+region+' '+machine['id'],flush=True)
    def probe(machine):
        output=run(['machine','exec',machine['id'],'curl --config /tmp/probe.conf','--app',app,'--timeout','180'],timeout=195)
        (root/(machine['region']+'-results.jsonl')).write_text(output)
        print('Captured '+machine['region'],flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(probe,m) for m in manifest['machines']]:
            future.result()
except BaseException as exc:
    manifest['error']=str(exc)
    print(type(exc).__name__+': '+str(exc),flush=True)
finally:
    if owned:
        try:
            manifest['cleanup']=run(['apps','destroy',app,'--yes'],timeout=60)
            manifest['cleanup_complete']=True
        except BaseException as exc:
            manifest['cleanup_error']=str(exc)
    manifest['finished_at']=time.time()
    save()
    print(json.dumps({k:v for k,v in manifest.items() if k != 'requests'}),flush=True)

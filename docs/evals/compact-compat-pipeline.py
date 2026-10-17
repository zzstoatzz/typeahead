import concurrent.futures,json,os,shlex,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent
machines=json.loads((root.parent/'qualification-shards.json').read_text())
def run(machine):
    shard=machine['shard']
    expected={i for i in range(180) if (i//3)%4==shard}
    for _ in range(1440):
        paths=list((root/'bounded-cancel-cache-controller'/f'shard-{shard}').glob('trial-*.json'))
        rows=[json.loads(p.read_text()) for p in paths]
        assert all(r['valid'] and r['exit_code']==0 and r['machine']==machine['machine'] for r in rows), 'Invalid cold trial; do not start compatibility'
        if {r['trial'] for r in rows}==expected:
            print(f'Shard {shard}: all 45 cold trials complete; starting independent compatibility',flush=True)
            break
        time.sleep(60)
    else: raise RuntimeError('Cold observation deadline; inspect existing controllers')
    env={'READ_POOL_SIZE':'8','QUAL_BINARY':'/app/typeahead-ingester','QUAL_BINARY_SHA256':'758d458b829f350531364982d24136b9e1097f4e4c34977996a37488ea9a3606','QUAL_CACHE':'/data/cache','QUAL_DEPLOYED_BINARY':'/data/deployed-release-72','QUAL_DEPLOYED_SHA256':'f82400c65efa59456b3240828da41e496ad2e92532318972fbf9356e226ea920','INDEX_R2_BUCKET':'typeahead-index/staging/compact-release-20261017/frozen-compat','EVAL_OUTPUT':'cancel-fixed-compat-correctness'}
    with (root.parent/f"compat-shard-{machine['shard']}.log").open('w') as log:
        command=shlex.join(['env',*(k+'='+v for k,v in env.items()),'python3','/data/qualification/compat_assay_pool8.py','correctness'])
        subprocess.run(['fly','ssh','console','-a','typeahead-search-compact','--machine',machine['machine'],'-C',command],stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run([sys.executable,str(root/'compat_controller.py')],env=dict(os.environ,QUAL_MACHINE=machine['machine'],QUAL_SHARD=str(machine['shard']),QUAL_SHARDS='4'),stdout=log,stderr=subprocess.STDOUT,check=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    futures=[pool.submit(run,m) for m in machines]
    for future in futures: future.result()
subprocess.run([sys.executable,str(root/'collect_parallel.py')],check=True)
subprocess.run([sys.executable,str(root/'collect_parallel.py'),'--compat'],check=True)
print('Combined legacy compatibility gate passed',flush=True)

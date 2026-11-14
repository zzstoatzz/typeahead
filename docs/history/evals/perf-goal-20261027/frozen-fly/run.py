"""Bounded local supervisor for one owned inactive-volume fork; never mutates either managed backend."""
import argparse,json,signal,subprocess,sys,time,uuid,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[4]/'scripts/perf'))
import fly_manager as fm
fm.PROTECTED='8e3d79b70dd748'
fm.BUILD='b1793109259-2b36-px05'
SOURCE_VOLUME='vol_v8eyqk61q9dy9gkv'

class Fork(fm.Manager):
    def __init__(self,image):
        run='20261027-'+uuid.uuid4().hex[:12]
        self.args=argparse.Namespace(run=run,image=image)
        self.root=Path(__file__).parent/run; self.root.mkdir(exist_ok=False)
        self.started=time.monotonic(); self.machine=None; self.volume=None; self.protected=None
        self.report={'run_id':run,'image':image,'goal_qualified':False,'events':[],'source_volume':SOURCE_VOLUME,'source_machine':fm.PROTECTED,'disk_budget_bytes':20*1024**3,'deadline_seconds':5100}
        self.api_origin='https://api.machines.dev'; self.test=False
        self.token_name='typeahead-perf-fork-'+(sys.argv[2] if len(sys.argv)>2 else run)
        result=json.loads(subprocess.check_output(['fly','tokens','create','deploy','-a',fm.APP,'--expiry','90m','--name',self.token_name,'--json'],text=True))
        self.token=result.get('token') if isinstance(result,dict) else result
        if not isinstance(self.token,str) or not self.token: raise RuntimeError('invalid scoped credential')
    def read(self,method,url,body=None,authorized=True):
        code="let s='';for await(const b of process.stdin)s+=b;const p=JSON.parse(s);const r=await fetch(p.url,{method:p.method,headers:{'User-Agent':'typeahead-perf/1.0','Content-Type':'application/json',...(p.token?{Authorization:'Bearer '+p.token}:{})},...(p.body===null?{}:{body:JSON.stringify(p.body)}),signal:AbortSignal.timeout(25000)});const raw=await r.text();console.log(JSON.stringify({status:r.status,raw}));"
        result=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps({'url':url,'method':method,'body':body,'token':self.token if authorized else None}),text=True,capture_output=True,timeout=30)
        if result.returncode: raise RuntimeError('control request transport failed')
        value=json.loads(result.stdout)
        if value['status']>=400: raise RuntimeError(f"control {method} HTTP {value['status']}")
        return json.loads(value['raw']) if value['raw'] and method!='DELETE' else None
    def api(self,method,path,body=None):
        return self.read(method,self.api_origin+'/v1/apps/'+fm.APP+'/'+path,body)
    def guard(self,deadline=True):
        route=self.read('GET','https://typeahead-search-transport.n8-3e9.workers.dev/route',authorized=False)
        if route!=fm.EXPECTED_ROUTE: raise RuntimeError('production route changed')
        protected=self.identity(self.api('GET','machines/'+fm.PROTECTED))
        if self.protected is not None and protected!=self.protected: raise RuntimeError('protected inactive Machine changed')
        self.protected=protected
        if deadline and time.monotonic()-self.started>5100: raise TimeoutError('private trial deadline')
    def resume(self,run):
        self.root.rmdir()
        self.root=Path(__file__).parent/run
        self.report=json.loads((self.root/'manager.json').read_text())
        self.args.run=run;self.args.image=self.report['image'];self.protected=self.report['protected']
        self.volume=self.api('GET','volumes/'+self.report['volume_id'])
        self.machine=self.api('GET','machines/'+self.report['machine_id'])
        self.started=time.monotonic()-900
        self.guard();self.owned()
        try:
            while True:
                self.guard()
                status=json.loads(self.execute('status'));self.save('resumed-private-trial',status=status)
                if status['phase']=='results-ready':self.collect(status);break
                time.sleep(25)
        finally:self.retire()
    def create(self):
        self.guard()
        if [m['id'] for m in self.api('GET','machines')]!=[fm.PROTECTED]: raise RuntimeError('inactive inventory changed')
        if self.protected['mounts'][0]['volume']!=SOURCE_VOLUME: raise RuntimeError('inactive volume changed')
        self.save('forking-independent-inactive-volume',protected=self.protected,creation_pending='volume')
        self.volume=self.api('POST','volumes',{'name':'perf_'+self.args.run.replace('-','_'),'source_volume_id':SOURCE_VOLUME,'region':'ewr','compute':fm.GUEST,'require_unique_zone':True})
        self.report['creation_pending']=None
        if self.volume['id']==SOURCE_VOLUME or self.volume.get('attached_machine_id'): raise RuntimeError('fork is not independent')
        self.save('fork-created',volume_id=self.volume['id'])
        deadline=time.monotonic()+900
        while self.api('GET','volumes/'+self.volume['id']).get('state') not in ('created','available'):
            self.guard()
            if time.monotonic()>deadline: raise TimeoutError('fork readiness')
            time.sleep(5)
        self.guard()
        self.save('creating-private-machine',creation_pending='machine')
        self.machine=self.api('POST','machines',{'name':'perf-'+self.args.run,'region':'ewr','skip_service_registration':True,'config':{'image':self.args.image,'guest':fm.GUEST,'services':[],'mounts':[{'volume':self.volume['id'],'path':'/data'}],'restart':{'policy':'no'},'stop_config':{'signal':'SIGTERM','timeout':20000000000},'env':{'PERF_RUN_ID':self.args.run,'REFRESH_BUILD_ID':fm.BUILD},'metadata':{'owner':'typeahead-perf-goal','perf_run':self.args.run,'build_id':fm.BUILD}}})
        self.report['creation_pending']=None
        self.save('private-machine-created',machine_id=self.machine['id']); self.owned()

def interrupted(*_):raise KeyboardInterrupt()
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
if __name__=='__main__':
    manager=Fork(sys.argv[1])
    try:
        manager.resume(sys.argv[2]) if len(sys.argv)>2 else manager.run()
    finally:
        rows=subprocess.check_output(['fly','tokens','list','-a',fm.APP,'--scope','app'],text=True).splitlines()
        matches=[row.split()[0] for row in rows if manager.token_name in row.split()]
        if len(matches)==1: subprocess.run(['fly','tokens','revoke',matches[0]],check=True,stdout=subprocess.DEVNULL)
        elif len(matches)>1: raise RuntimeError('ambiguous scoped token retirement')

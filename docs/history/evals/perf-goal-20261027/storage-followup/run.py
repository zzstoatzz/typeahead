"""Bounded local supervisor for one owned inactive-volume fork; never mutates either managed backend."""
import argparse,json,signal,subprocess,sys,time,uuid,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[4]/'scripts/perf'))
import fly_manager as fm
fm.APP='typeahead-search-refresh'
fm.PROTECTED='801539a6551778'
fm.EXPECTED_ROUTE={'origin':'https://typeahead-startup-20261024.fly.dev','build_id':'b1793109259-2b36-px05'}
fm.BUILD='b1793081565-0228-px05'
SOURCE_VOLUME='vol_40o3xy5812d6kkk4'

class Fork(fm.Manager):
    def __init__(self,image):
        run='20261027-'+uuid.uuid4().hex[:12]
        self.args=argparse.Namespace(run=run,image=image)
        self.root=Path(__file__).parent/run; self.root.mkdir(exist_ok=False)
        self.started=time.monotonic(); self.machine=None; self.volume=None; self.protected=None
        self.report={'run_id':run,'image':image,'goal_qualified':False,'events':[],'source_volume':SOURCE_VOLUME,'source_machine':fm.PROTECTED,'disk_budget_bytes':20*1024**3,'deadline_seconds':5100}
        self.api_origin='https://api.machines.dev'; self.test=False
        self.serving=None
        self.guard_token=None
        self.guard_token_name='typeahead-perf-read-'+run
        self.token_name='typeahead-perf-fork-'+(sys.argv[2] if len(sys.argv)>2 else run)
        result=json.loads(subprocess.check_output(['fly','tokens','create','deploy','-a',fm.APP,'--expiry','90m','--name',self.token_name,'--json'],text=True))
        self.token=result.get('token') if isinstance(result,dict) else result
        if not isinstance(self.token,str) or not self.token: raise RuntimeError('invalid scoped credential')
    def read(self,method,url,body=None,authorized=True,serving_guard=False):
        code="let s='';for await(const b of process.stdin)s+=b;const p=JSON.parse(s);const r=await fetch(p.url,{method:p.method,headers:{'User-Agent':'typeahead-perf/1.0','Content-Type':'application/json',...(p.token?{Authorization:'Bearer '+p.token}:{})},...(p.body===null?{}:{body:JSON.stringify(p.body)}),signal:AbortSignal.timeout(25000)});const raw=await r.text();console.log(JSON.stringify({status:r.status,raw}));"
        result=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps({'url':url,'method':method,'body':body,'token':self.guard_token if serving_guard else self.token if authorized else None}),text=True,capture_output=True,timeout=30)
        if result.returncode: raise RuntimeError('control request transport failed')
        value=json.loads(result.stdout)
        if value['status']>=400: raise RuntimeError(f"control {method} HTTP {value['status']}")
        return json.loads(value['raw']) if value['raw'] and method!='DELETE' else None
    def api(self,method,path,body=None):
        return self.read(method,self.api_origin+'/v1/apps/'+fm.APP+'/'+path,body)
    def guard(self,deadline=True):
        route=self.read('GET','https://typeahead-search-transport.n8-3e9.workers.dev/route',authorized=False)
        if route!=fm.EXPECTED_ROUTE: raise RuntimeError('production route changed')
        if self.guard_token is None:
            credential=json.loads(subprocess.check_output(['fly','tokens','create','readonly','--org','personal','--expiry','90m','--name',self.guard_token_name,'--json'],text=True))
            self.guard_token=credential.get('token') if isinstance(credential,dict) else credential
            if not isinstance(self.guard_token,str) or not self.guard_token: raise RuntimeError('invalid guard credential')
        serving=self.identity(self.read('GET',self.api_origin+'/v1/apps/typeahead-startup-20261024/machines/8e3d79b70dd748',serving_guard=True))
        if self.serving is not None and serving!=self.serving: raise RuntimeError('serving Machine changed')
        if serving['instance_id']!='01M5Z6NJ6RNKFMHR9FS8P0MBVN' or serving['state']!='started': raise RuntimeError('serving identity differs from preflight')
        self.serving=serving
        self.report['serving_guard']=serving
        protected=self.identity(self.api('GET','machines/'+fm.PROTECTED))
        if self.protected is not None and protected!=self.protected: raise RuntimeError('protected inactive Machine changed')
        if protected['instance_id']!='01M5Y5TRPWTXKDFZKZ4VYRH5CZ' or protected['state']!='started': raise RuntimeError('inactive identity differs from preflight')
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
        if {m['id'] for m in self.api('GET','machines')}!={fm.PROTECTED}: raise RuntimeError('inactive inventory changed')
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
        errors=[]
        for scope,name,args in [('app',manager.token_name,['-a',fm.APP,'--scope','app']),('org',manager.guard_token_name,['--org','personal','--scope','org'])]:
            try:
                rows=subprocess.check_output(['fly','tokens','list',*args],text=True).splitlines()
                matches=[row.split()[0] for row in rows if name in row.split()]
                if len(matches)==1: subprocess.run(['fly','tokens','revoke',matches[0]],check=True,stdout=subprocess.DEVNULL)
                elif len(matches)>1: raise RuntimeError('ambiguous '+scope+' token retirement')
            except Exception as error: errors.append(scope+': '+str(error))
        manager.report['credential_retirement_errors']=errors
        manager.save('retired-private-resources' if manager.report.get('retired') else 'credential-cleanup-complete')
        if errors: raise RuntimeError('; '.join(errors))

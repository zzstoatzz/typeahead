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
        self.token=subprocess.check_output(['fly','auth','token','--quiet'],text=True).strip()
    def api(self,method,path,body=None):
        request=urllib.request.Request(self.api_origin+'/v1/apps/'+fm.APP+'/'+path,data=None if body is None else json.dumps(body).encode(),method=method,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','User-Agent':'typeahead-perf/1.0'})
        with urllib.request.urlopen(request,timeout=25) as response:
            raw=response.read(2*1024*1024)
            return json.loads(raw) if raw and method!='DELETE' else None
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
if __name__=='__main__': Fork(sys.argv[1]).run()

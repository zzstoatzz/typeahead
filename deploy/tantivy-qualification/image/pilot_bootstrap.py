"""Tiny synthetic cloud lifecycle pilot; never opens a live source or production disk."""
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import threading
import time

ROOT=Path('/data/qualification/run')

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()

def prepare(observer,watermark,nonce):
    role=os.environ['QUAL_ROLE'];arm={'native-control':'native','tantivy-control':'tantivy'}[role]
    if ROOT.exists():raise ValueError('pilot scratch already exists')
    ROOT.mkdir(parents=True);(ROOT/'owner').write_text('native-qualification-v1')
    actors=[{'type':'actor','did':f'did:fixture:{i:02}','handle':f'g{i:02}.fixture.test','display_name':f'Actor {i}',
             'avatar_url':'','labels':[],'pds':'https://local.test','created_at':'','associated':{},'hidden':False,
             'deleted':False,'quality_score':i,'version':watermark} for i in range(32)]
    changed=actors[0]|{'handle':'renamed.fixture.test','display_name':'Renamed Actor','quality_score':100,'version':watermark+1}
    feed={'cursor':1,'expected_cursor':0,'actors':[changed]}
    feedsha=digest(feed);corpus=digest(actors)
    (ROOT/'observer.json').write_text(json.dumps(observer))
    config={'schema':1,'arm':arm,'root':str(ROOT),'app':observer['app'],'machine_id':observer['machine_id'],
            'guest':observer['guest'],'volume_id':observer['volume_ids'][0],'final_volume_gib':20,
            'nonce':nonce,'corpus_sha256':corpus,'updates_sha256':feedsha,'observer_receipt':str(ROOT/'observer.json'),
            'private_listen':'::','measurement_deadline_s':min(300,max(1,int(float(os.environ['QUAL_DEADLINE'])-time.time()))),
            'paths':{k:str(ROOT/v) for k,v in [('overlay','overlay.db'),('tmp','tmp'),('request','request.json'),('response','response.json'),('tantivy_data','tantivy')]}}
    if arm=='native':
        source=ROOT/'source.db'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(a['did'],a['handle'],a['display_name'],'','[]',a['pds'],'','{}',0,a['quality_score'],watermark) for a in actors])
        env={**os.environ,'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0',
             'INDEX_PDS_MAX_ACTORS':'10000','INDEX_REUSE_SOURCE':str(source),'INDEX_BUILD_ROOT':str(ROOT/'build'),'INDEXER_VERSION':'qualification-pilot'}
        with (ROOT/'builder.log').open('w') as log:subprocess.run(['/app/bin/typeahead-ingester'],env=env,stdout=log,stderr=log,check=True,timeout=60)
        snapshot=next((ROOT/'build').glob('build-*/index.db'));config['paths']['snapshot']=str(snapshot)
        with sqlite3.connect(snapshot) as db:
            floor=int(db.execute("SELECT value FROM index_meta WHERE key='source_watermark'").fetchone()[0])
            marker=db.execute("SELECT value FROM index_meta WHERE key='flat_prefix_sha256'").fetchone()
        if floor!=watermark:raise ValueError('native snapshot watermark differs from frozen source')
        if marker:
            subprocess.run(['/app/bin/admit-prefix'],env={**os.environ,'TYPEAHEAD_ADMIT_PREFIX_PATH':str(snapshot)+'.prefix',
                           'TYPEAHEAD_ADMIT_PREFIX_SHA256':marker[0]},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=20)
    else:
        lines=[encoded(a)+b'\n' for a in actors]
        header={'type':'manifest','schema_version':1,'build_id':'fixture-qualification-pilot','source_watermark':watermark,
                'watermark':str(watermark),'actor_count':32,'source_actor_count':32,'normalize_version':'v2-cjk-bigram',
                'scoring_version':'v1-quality-score','source_format':'synthetic-operational-fixture','synthetic':True,'partial':False}
        footer={'type':'end','build_id':header['build_id'],'actor_count':32,'actors_sha256':hashlib.sha256(b''.join(lines)).hexdigest()}
        stream=ROOT/'input.jsonl';stream.write_bytes(encoded(header)+b'\n'+b''.join(lines)+encoded(footer)+b'\n')
        subprocess.run(['/app/bin/typeahead-tantivy','import','--data',config['paths']['tantivy_data'],'--input',str(stream),
                        '--writer-memory-mb','32','--batch-size','32'],stdout=subprocess.DEVNULL,check=True,timeout=60)
        stream.unlink()
    (ROOT/'run.json').write_text(json.dumps(config))
    return config,feed,feedsha

class Server(http.server.ThreadingHTTPServer):address_family=socket.AF_INET6;daemon_threads=True

def main():
    deadline=float(os.environ['QUAL_DEADLINE'])
    if not time.time()<deadline<=time.time()+600:raise ValueError('pilot lease invalid')
    def expired(*_):raise TimeoutError('pilot hard deadline')
    signal.signal(signal.SIGALRM,expired);signal.alarm(max(1,int(deadline-time.time())))
    def terminate(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate)
    observer=json.loads(os.environ['QUAL_OBSERVER_JSON']);nonce=os.environ['QUAL_PILOT_NONCE']
    config,feed,feedsha=prepare(observer,int(os.environ['QUAL_WATERMARK']),nonce)
    child=None;lock=threading.Lock()
    def start():return subprocess.Popen(['python3','/app/entrypoint.py','serve'],start_new_session=True)
    def stop():
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
    class Control(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal child
            if self.headers.get('X-Pilot-Authorization')!=os.environ['QUAL_CONTROL_SECRET'] or self.headers.get('Content-Length','0')!='0':self.send_error(403);return
            try:
                with lock:
                    if self.path=='/restart':stop();child=start();value={'restarted':True}
                    elif self.path=='/apply':
                        if config['arm']=='native':
                            request=ROOT/'request.json';response=ROOT/'response.json'
                            response.unlink(missing_ok=True)
                            native_actors=[{k:(json.dumps(v,separators=(',',':')) if k in {'labels','associated'} else v)
                                            for k,v in actor.items() if k!='type'} for actor in feed['actors']]
                            part=ROOT/'request.part';part.write_bytes(encoded(feed|{'actors':native_actors,'feed_sha256':feedsha}));part.replace(request)
                            until=time.monotonic()+10
                            while request.exists():
                                if time.monotonic()>until:raise TimeoutError('native commit acknowledgement')
                                time.sleep(.02)
                            value=json.loads(response.read_text())
                            if value.get('accepted') is not True:raise ValueError('native batch rejected')
                        else:
                            conn=http.client.HTTPConnection('127.0.0.1',18081,timeout=10)
                            try:
                                conn.request('POST','/admin/apply',encoded({'actors':feed['actors'],'watermark':int(os.environ['QUAL_WATERMARK'])+1}),
                                             {'Content-Type':'application/json','Authorization':'Bearer '+os.environ['ADMIN_SECRET']})
                                response=conn.getresponse();raw=response.read(65536)
                                if response.status!=200:raise ValueError('Tantivy batch rejected')
                                value=json.loads(raw)
                            finally:conn.close()
                    else:self.send_error(404);return
                body=encoded(value);self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except Exception:self.send_error(503,'pilot action failed')
        def log_message(self,*args):pass
    try:
        child=start();server=Server(('::',8082),Control);server.timeout=.5
        print(json.dumps({'event':'PILOT_PREPARED','arm':config['arm'],'actors':32,'corpus_sha256':config['corpus_sha256'],'updates_sha256':feedsha}),flush=True)
        while time.time()<deadline:
            with lock:
                if child.poll() is not None:raise RuntimeError('pilot runtime stopped')
            server.handle_request()
    finally:
        if 'server' in locals():server.server_close()
        stop()

if __name__=='__main__':main()

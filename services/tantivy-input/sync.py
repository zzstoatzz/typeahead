#!/usr/bin/env python3
"""Read-only Turso keyset replay into the isolated localhost Tantivy admin API."""
from contextlib import contextmanager
import argparse
import copy
import http.client
import hashlib
import sqlite3
import re
import json
import math
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACTOR_COLUMNS=('did','handle','display_name','avatar_url','hidden','labels','created_at','associated','pds','followers_count','follows_count','posts_count','last_activity_at','quality_score','updated_at')
ACTOR_TUPLE_SQL=tuple("CASE WHEN quality_score IS NULL THEN NULL WHEN typeof(quality_score) IN ('integer','real') THEN printf('%!.17g', quality_score) ELSE quality_score END" if column=='quality_score' else column for column in ACTOR_COLUMNS)
CLOCK_SQL='SELECT unixepoch() AS source_now'
ACTORS_SQL='SELECT '+', '.join(ACTOR_COLUMNS)+' FROM actors WHERE updated_at > ?1 AND updated_at >= ?2 AND (updated_at > ?2 OR did > ?3) AND updated_at <= ?4 ORDER BY updated_at, did LIMIT ?5'
TOMBSTONES_SQL="SELECT t.did, t.deleted_at, CASE WHEN a.handle <> '' AND a.updated_at >= t.deleted_at AND a.updated_at <= ?4 THEN 1 ELSE 0 END AS superseded FROM tombstones AS t LEFT JOIN actors AS a ON a.did = t.did WHERE t.deleted_at > ?1 AND t.deleted_at >= ?2 AND (t.deleted_at > ?2 OR t.did > ?3) AND t.deleted_at <= ?4 ORDER BY t.deleted_at, t.did LIMIT ?5"
CURRENT_ROWS_SQL = ('WITH requested AS (SELECT value AS did FROM json_each(?1)) SELECT requested.did AS requested_did, unixepoch() AS source_now, '
    + ', '.join('a.' + column for column in ACTOR_COLUMNS)
    + ' FROM requested LEFT JOIN actors AS a ON a.did = requested.did ORDER BY requested.did')
CURRENT_SQL = ('SELECT json_group_array(json_array('
    + ', '.join(['requested_did', 'source_now', *ACTOR_TUPLE_SQL])
    + ')) AS observations_json FROM (' + CURRENT_ROWS_SQL + ')')
SOURCE_WALK_SQL = 'SELECT did FROM actors WHERE did > ?1 ORDER BY did LIMIT ?2'
CURRENT_POLICY_SQL = (
    'WITH requested AS (SELECT value AS did FROM json_each(?1)), '
    'profile_rows AS (SELECT requested.did AS requested_did, '
    + ', '.join('a.' + column for column in ACTOR_COLUMNS)
    + ' FROM requested LEFT JOIN actors AS a ON a.did = requested.did ORDER BY requested.did), '
    'policy_overrides AS (SELECT did, action FROM mod_overrides ORDER BY did LIMIT 10001), '
    'policy_domains AS (SELECT domain FROM blocked_domains ORDER BY domain LIMIT 10001) '
    'SELECT (SELECT json_group_array(json_array('
    + ', '.join(['requested_did', *ACTOR_TUPLE_SQL])
    + ')) FROM profile_rows) AS actors_json, '
    "(SELECT json_group_array(json_object('did', did, 'action', action)) FROM policy_overrides) AS overrides_json, "
    '(SELECT json_group_array(domain) FROM policy_domains) AS domains_json, unixepoch() AS source_now'
)
SELECTS={CLOCK_SQL,ACTORS_SQL,TOMBSTONES_SQL,CURRENT_SQL,SOURCE_WALK_SQL,CURRENT_POLICY_SQL}
RETENTION_SECONDS=7*24*3600
SOURCE_POLICY='turso-current-state-cutoff-v1'

class SafeError(Exception):pass
class SourceBudgetExhausted(SafeError):pass
class SweepBudgetExhausted(SafeError):pass
class Retryable(SafeError):pass
class CursorConflict(SafeError):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise SafeError('HTTP redirects are prohibited')

def json_bytes(value):return json.dumps(value,separators=(',',':'),sort_keys=True,allow_nan=False).encode()
def number(value,name):
    if type(value) is not int or not 0<=value<2**63:raise SafeError('invalid '+name)
    return value

def local_url(value):
    p=urllib.parse.urlsplit(value)
    if p.scheme!='http' or p.hostname not in ['127.0.0.1','localhost','::1'] or p.username or p.password or p.query or p.fragment:raise SafeError('admin/status target must be explicit HTTP loopback')
    return value.rstrip('/')

def source_url(value,allow_test_loopback=False):
    if value.startswith('libsql://'):value='https://'+value[len('libsql://'):]
    p=urllib.parse.urlsplit(value)
    test=allow_test_loopback and p.scheme=='http' and p.hostname in ['127.0.0.1','::1','localhost']
    if (p.scheme!='https' and not test) or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ['','/','/v2/pipeline']:raise SafeError('Turso source must be an HTTPS origin without embedded credentials')
    return urllib.parse.urlunsplit((p.scheme,p.netloc,'/v2/pipeline','',''))

class Http:
    def __init__(self,timeout=15):
        self.timeout=timeout;self.opener=urllib.request.build_opener(NoRedirect());self.received_bytes=0
    def request(self,url,secret,body=None,max_bytes=4*1024*1024):
        if not secret or '\n' in secret or '\r' in secret:raise SafeError('missing/invalid bearer credential')
        data=None if body is None else json_bytes(body)
        if data is not None and len(data)>16*1024*1024:raise SafeError('admin request exceeds16MiB')
        req=urllib.request.Request(url,data=data,headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json','User-Agent':'typeahead-tantivy-readonly-sync/1'})
        try:
            deadline=time.monotonic()+self.timeout
            with self.opener.open(req,timeout=self.timeout) as response:
                payload=bytearray()
                while True:
                    if time.monotonic()>deadline:raise Retryable('HTTP response exceeded elapsed deadline')
                    chunk=response.read1(min(65536,max_bytes+1-len(payload)))
                    self.received_bytes+=len(chunk)
                    if not chunk:break
                    payload.extend(chunk)
                    if len(payload)>max_bytes:raise SafeError('HTTP response exceeds bounded size')
                try:return json.loads(payload),len(payload)
                except (ValueError,UnicodeError):raise SafeError('invalid bounded JSON response') from None
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            if code==409:raise CursorConflict('admin rejected source cursor or source version conflict') from None
            if code in [408,429,500,502,503,504]:raise Retryable('transient HTTP failure '+str(code)) from None
            raise SafeError('HTTP request rejected '+str(code)) from None
        except (urllib.error.URLError,TimeoutError,socket.timeout,http.client.HTTPException,OSError):
            raise Retryable('HTTP transport failure') from None

class Turso:
    def __init__(self,url,token,http=None,allow_test_loopback=False,retries=3,backoff=.5,max_bytes=512*1024*1024,poll_reserve_bytes=None):
        if type(max_bytes) is not int or max_bytes<2:raise SafeError('invalid source byte budget')
        reserve=max_bytes//4 if poll_reserve_bytes is None else poll_reserve_bytes
        if type(reserve) is not int or not 0<=reserve<=max_bytes:raise SafeError('invalid polling reserve')
        self.url=source_url(url,allow_test_loopback);self.token=token;self.http=http or Http();self.retries=retries;self.backoff=backoff;self.max_bytes=max_bytes;self.bytes=0;self.requests=0
        self.poll_reserve_bytes=reserve;self.poll_bytes=0;self.sweep_bytes=0;self.budget_kind='poll'
    @contextmanager
    def budget_scope(self,kind):
        if kind not in ['poll','sweep']:raise SafeError('unknown source budget scope')
        previous=self.budget_kind;self.budget_kind=kind
        try:yield
        finally:self.budget_kind=previous
    def remaining_bytes(self):
        total=self.max_bytes-self.bytes
        if self.budget_kind=='sweep':return min(total,self.max_bytes-self.poll_reserve_bytes-self.sweep_bytes)
        return total
    def accounting(self):
        return {'source_bytes':self.bytes,'poll_bytes':self.poll_bytes,'sweep_bytes':self.sweep_bytes,'max_source_bytes':self.max_bytes,'poll_reserve_bytes':self.poll_reserve_bytes,'sweep_ceiling_bytes':self.max_bytes-self.poll_reserve_bytes,'remaining_source_bytes':max(0,self.max_bytes-self.bytes),'remaining_sweep_bytes':max(0,min(self.max_bytes-self.bytes,self.max_bytes-self.poll_reserve_bytes-self.sweep_bytes))}

    def _before_request(self,max_response_bytes):pass
    def _after_request(self):pass

    def select(self,sql,args):
        if sql not in SELECTS:raise SafeError('only fixed source SELECTs are permitted')
        values=[]
        for arg in args:
            if type(arg) is int:values.append({'type':'integer','value':str(arg)})
            elif isinstance(arg,str):values.append({'type':'text','value':arg})
            else:raise SafeError('unsupported bound source parameter')
        body={'requests':[{'type':'execute','stmt':{'sql':sql,'args':values,'want_rows':True}},{'type':'close'}]}
        for attempt in range(self.retries):
            try:
                remaining=self.remaining_bytes()
                if remaining<=1:
                    if self.budget_kind=='sweep':raise SweepBudgetExhausted('sweep byte budget exhausted; polling reserve retained')
                    raise SourceBudgetExhausted('source byte budget exhausted')
                before=self.http.received_bytes;self.requests+=1
                response_limit=min(4*1024*1024,remaining-1)
                self._before_request(response_limit+1)
                try:
                    result,size=self.http.request(self.url,self.token,body,max_bytes=response_limit)
                except SafeError:
                    if self.http.received_bytes-before>=remaining:
                        if self.budget_kind=='sweep':raise SweepBudgetExhausted('sweep byte budget exhausted; polling reserve retained') from None
                        raise SourceBudgetExhausted('source byte budget exhausted') from None
                    raise
                finally:
                    received=self.http.received_bytes-before;self.bytes+=received
                    if self.budget_kind=='sweep':self.sweep_bytes+=received
                    else:self.poll_bytes+=received
                    self._after_request()
                break
            except Retryable:
                if attempt+1==self.retries:raise
                time.sleep(min(4,self.backoff*2**attempt))
        else:raise SafeError('no source request attempted')
        try:
            execute=result['results'][0]
            if execute['type']!='ok' or execute['response']['type']!='execute':raise SafeError('source execute failed')
            payload=execute['response']['result']
            if payload.get('affected_row_count',0)!=0:raise SafeError('source SELECT unexpectedly affected rows')
            columns=[c['name'] for c in payload['cols']]
            expected=(['source_now'] if sql==CLOCK_SQL else list(ACTOR_COLUMNS) if sql==ACTORS_SQL else ['observations_json'] if sql==CURRENT_SQL else ['actors_json','overrides_json','domains_json','source_now'] if sql==CURRENT_POLICY_SQL else ['did'] if sql==SOURCE_WALK_SQL else ['did','deleted_at','superseded'])
            if columns!=expected:raise SafeError('source response column contract changed')
            rows=[]
            for cells in payload['rows']:
                if len(cells)!=len(columns):raise SafeError('source row width differs')
                decoded=[]
                for cell in cells:
                    kind=cell['type'];value=cell.get('value')
                    if kind=='null':decoded.append(None)
                    elif kind=='integer':decoded.append(int(value))
                    elif kind=='float':decoded.append(float(value))
                    elif kind=='text' and isinstance(value,str):decoded.append(value)
                    else:raise SafeError('unsupported source value type')
                rows.append(dict(zip(columns,decoded)))
            if sql not in [CLOCK_SQL,CURRENT_SQL,CURRENT_POLICY_SQL] and len(rows)>args[-1]:raise SafeError('source page exceeds requested row bound')
            return rows
        except (KeyError,TypeError,ValueError,IndexError):raise SafeError('malformed source response') from None
    def now(self):
        rows=self.select(CLOCK_SQL,[])
        if len(rows)!=1:raise SafeError('source clock response differs')
        return number(rows[0]['source_now'],'source clock')

class Admin:
    def __init__(self,url,secret,status_url=None,http=None):
        self.url=local_url(url)
        if urllib.parse.urlsplit(self.url).path not in ['','/']:raise SafeError('admin URL must be a loopback origin')
        self.status_url=local_url(status_url or self.url+'/index/status');self.secret=secret;self.http=http or Http()
    def status(self):return self.http.request(self.status_url,self.secret,max_bytes=256*1024)[0]
    def apply(self,actors,previous,next_cursor,watermark=None):
        body={'actors':actors,'expected_source_cursor':previous,'source_cursor':next_cursor}
        if watermark is not None:body['watermark']=watermark
        return self.http.request(self.url+'/admin/apply',self.secret,body,max_bytes=256*1024)[0]

def text(row,key,default=''):
    value=row.get(key)
    if value is None:return default
    if not isinstance(value,str):raise SafeError('source text type differs: '+key)
    return value

def deletion(did,version):
    return {'did':did,'version':version,'deleted':True,'hidden':False,'handle':'','display_name':'','avatar_url':'','labels':[],'associated':{},'created_at':'','pds':'','quality_score':0.0,'followers_count':0,'follows_count':0,'posts_count':0,'last_activity_at':0}

class PdsFallback:
    def __init__(self, path, sha256):
        self.path=Path(path).resolve()
        if not re.fullmatch(r'[a-f0-9]{64}',sha256):raise SafeError('invalid fallback digest')
        with self.path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=sha256:raise SafeError('fallback checksum differs')
        self.sha256=sha256
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            if self.db.execute('PRAGMA user_version').fetchone()[0]!=1:raise SafeError('unsupported fallback schema')
            columns=self.db.execute('PRAGMA table_info(pds_fallback)').fetchall()
            if [(r[1],r[2],r[5]) for r in columns]!=[('did','TEXT',1),('pds','TEXT',0)]:raise SafeError('invalid fallback schema')
        except BaseException:
            self.db.close();raise

    def lookup(self, did):
        row=self.db.execute('SELECT pds FROM pds_fallback WHERE did=?',(did,)).fetchone()
        if row is None:return ''
        pds=row[0]
        if canonical_archive_pds(pds)!=pds:raise SafeError('invalid archive PDS')
        return pds

    def close(self):self.db.close()


def canonical_archive_pds(value):
    if not isinstance(value,str):return None
    try:url=urllib.parse.urlsplit(value)
    except ValueError:return None
    host=url.hostname or ''
    if (url.scheme!='https' or url.netloc!=host or url.path or url.query or url.fragment
        or host in {'unknown','invalid-pds','bsky.network','relay1.us-east.bsky.network','bridgy-hubble.microcosm.blue'}
        or len(host)>253 or '.' not in host
        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in host.split('.'))):return None
    return 'https://'+host


_active_pds_fallback=None

@contextmanager
def pds_fallback(path, sha256):
    global _active_pds_fallback
    if _active_pds_fallback is not None:raise SafeError('fallback already configured')
    fallback=PdsFallback(path,sha256);_active_pds_fallback=fallback
    try:yield fallback
    finally:_active_pds_fallback=None;fallback.close()


def actor_record(row):
    did=text(row,'did');version=number(row.get('updated_at'),'actor source version')
    if not did.startswith('did:') or len(did.encode())>2048:raise SafeError('invalid source actor DID')
    if not text(row,'handle'):return deletion(did,version)
    score=row.get('quality_score') or 0
    try:score=struct.unpack('<f',struct.pack('<f',float(score)))[0]
    except (ValueError,TypeError,OverflowError,struct.error):raise SafeError('source quality score is not finite f32') from None
    if not math.isfinite(score):raise SafeError('source quality score is not finite f32')
    result={'did':did,'version':version,'deleted':False,'hidden':bool(row.get('hidden')),'quality_score':score}
    for field in ['handle','display_name','avatar_url','created_at','pds']:result[field]=text(row,field)
    if not result['pds'] and _active_pds_fallback is not None:
        result['pds']=_active_pds_fallback.lookup(did)
    for field,kind,default in [('labels',list,'[]'),('associated',dict,'{}')]:
        try:value=json.loads(text(row,field,default))
        except ValueError:raise SafeError('invalid source hydration JSON: '+field) from None
        if not isinstance(value,kind):raise SafeError('invalid source hydration JSON shape: '+field)
        result[field]=value
    for field in ['followers_count','follows_count','posts_count','last_activity_at']:
        value=row.get(field) or 0
        if type(value) is not int or not -2**63<=value<2**63:raise SafeError('source count is not signed64')
        result[field]=value
    return result

def initial_cursor(status,lookback_seconds=300):
    if status.get('import_complete') is not True:raise SafeError('initial import is not complete')
    build=status.get('build_id')
    if not isinstance(build,str) or not build or len(build)>128:raise SafeError('missing backend build identity')
    floor=number(status.get('snapshot_watermark'),'snapshot watermark')
    cursor=status.get('source_cursor')
    if cursor is None:return {'schema_version':1,'build_id':build,'snapshot_watermark':floor,'actors':{'updated_at':floor,'did':''},'tombstones':{'deleted_at':floor,'did':''},'through':floor,'cycle':None,'lookback_seconds':lookback_seconds,'source_policy':SOURCE_POLICY}
    if not isinstance(cursor,dict) or cursor.get('schema_version')!=1 or cursor.get('build_id')!=build or cursor.get('snapshot_watermark')!=floor:raise SafeError('source cursor belongs to a different snapshot/schema')
    stored_policy=cursor.get('source_policy')
    if stored_policy not in [None,SOURCE_POLICY]:raise SafeError('durable replay source policy differs')
    stored_lookback=cursor.get('lookback_seconds',lookback_seconds)
    if type(stored_lookback) is not int or stored_lookback!=lookback_seconds:raise SafeError('durable replay lookback policy differs')
    through=number(cursor.get('through'),'durable through')
    if through<floor:raise SafeError('durable source watermark is below snapshot')
    for stream,column in [('actors','updated_at'),('tombstones','deleted_at')]:
        position=cursor.get(stream,{})
        if number(position.get(column),'source keyset timestamp')<floor or not isinstance(position.get('did'),str) or len(position['did'].encode())>2048:raise SafeError('invalid durable source keyset')
    cycle=cursor.get('cycle')
    if cycle is not None and (not isinstance(cycle,dict) or cycle.get('stream') not in ['actors','tombstones'] or number(cycle.get('upper_bound'),'source cutoff')<through):raise SafeError('invalid durable source cycle')
    result=copy.deepcopy(cursor);result['lookback_seconds']=stored_lookback;result['source_policy']=SOURCE_POLICY;return result

class Synchronizer:
    def __init__(self,source,admin,page_size=500,max_pages=256,max_seconds=300,safety_lag=2,retries=3,backoff=.5,status_file=None,lookback_seconds=300):
        if not 1<=page_size<=1000 or not 1<=max_pages<=10000 or not 0<=safety_lag<=60 or not 0<=lookback_seconds<=3600 or not math.isfinite(max_seconds) or max_seconds<=0:raise SafeError('invalid bounded replay settings')
        self.source=source;self.admin=admin;self.page_size=page_size;self.max_pages=max_pages;self.max_seconds=max_seconds;self.safety_lag=safety_lag;self.retries=retries;self.backoff=backoff;self.status_file=status_file;self.lookback_seconds=lookback_seconds
    def committed(self,actors,previous,next_cursor,watermark):
        for attempt in range(self.retries):
            try:
                reply=self.admin.apply(actors,previous,next_cursor,watermark)
                if reply.get('source_cursor')!=next_cursor:raise SafeError('admin acknowledgement did not match committed cursor')
                return reply
            except Retryable:
                try:observed=self.admin.status()
                except Retryable:observed=None
                if observed is not None:
                    if observed.get('source_cursor')==next_cursor:return {'source_cursor':next_cursor,'ack_recovered':True}
                    if observed.get('source_cursor')!=previous:raise CursorConflict('source cursor changed during ambiguous commit')
                if attempt+1==self.retries:raise
                time.sleep(min(4,self.backoff*2**attempt))
        raise SafeError('no commit attempted')
    def save(self,result):
        if self.status_file:
            p=Path(self.status_file);temporary=p.with_name(p.name+'.tmp')
            with temporary.open('w') as f:json.dump(result,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
            os.replace(temporary,p)
    def run(self):
        started=time.monotonic();status=self.admin.status();cursor=initial_cursor(status,self.lookback_seconds);previous=status.get('source_cursor')
        now=self.source.now()
        if now<cursor['through']:raise SafeError('source clock is behind durable watermark')
        if now-cursor['through']>RETENTION_SECONDS:raise SafeError('source gap exceeds tombstone retention; require fresh snapshot')
        if cursor['cycle'] is None:
            upper=now-self.safety_lag
            if upper<cursor['through']:return {'phase':'waiting-for-safe-source-clock','caught_up':False,'through':cursor['through']}
            anchor=max(cursor['snapshot_watermark'],cursor['through']-cursor['lookback_seconds'])
            cursor['actors']={'updated_at':anchor,'did':''};cursor['tombstones']={'deleted_at':anchor,'did':''}
            cursor['cycle']={'upper_bound':upper,'stream':'actors'}
        pages=records=submitted_records=applied=replayed=stale=ack_recoveries=superseded_tombstones=0
        result={'phase':'bounded-pause','caught_up':False,'through':cursor['through'],'source_cursor':cursor,'pages':0,'records':0,'submitted_records':0,'superseded_tombstones':0,'source_policy':SOURCE_POLICY,'source_requests':self.source.requests,'source_bytes':self.source.bytes,'wall_seconds':time.monotonic()-started}
        while pages<self.max_pages and time.monotonic()-started<self.max_seconds:
            stream=cursor['cycle']['stream'];column='updated_at' if stream=='actors' else 'deleted_at';position=cursor[stream];upper=cursor['cycle']['upper_bound']
            sql=ACTORS_SQL if stream=='actors' else TOMBSTONES_SQL
            rows=self.source.select(sql,[cursor['snapshot_watermark'],position[column],position['did'],upper,self.page_size]);pages+=1
            next_cursor=copy.deepcopy(cursor);actors=[];superseded_page=0;last=(position[column],position['did'].encode())
            for row in rows:
                stamp=number(row.get(column),'source row timestamp');did=text(row,'did');key=(stamp,did.encode())
                if not did.startswith('did:') or not cursor['snapshot_watermark']<stamp<=upper or key<=last:raise SafeError('source keyset page violates ordered cutoff')
                if stream=='tombstones':
                    flag=row.get('superseded')
                    if type(flag) is not int or flag not in [0,1]:raise SafeError('invalid retained-tombstone superseded flag')
                    if flag:superseded_page+=1
                    else:actors.append(deletion(did,stamp))
                else:actors.append(actor_record(row))
                last=key
            watermark=None
            if rows:next_cursor[stream]={column:last[0],'did':last[1].decode()}
            else:
                next_cursor[stream]={column:upper,'did':''}
                if stream=='actors':next_cursor['cycle']['stream']='tombstones'
                else:next_cursor.update(cycle=None,through=upper);watermark=upper
            reply=self.committed(actors,previous,next_cursor,watermark);records+=len(rows);submitted_records+=len(actors);superseded_tombstones+=superseded_page
            applied+=reply.get('applied',0);replayed+=reply.get('replayed',0);stale+=reply.get('stale',0);ack_recoveries+=int(reply.get('ack_recovered',False))
            previous=copy.deepcopy(next_cursor);cursor=next_cursor
            result={'phase':'caught-up' if cursor['cycle'] is None else 'replaying','caught_up':cursor['cycle'] is None,'through':cursor['through'],'source_cutoff':upper,'pages':pages,'records':records,'submitted_records':submitted_records,'superseded_tombstones':superseded_tombstones,'source_policy':SOURCE_POLICY,'applied':applied,'replayed':replayed,'stale':stale,'ack_recoveries':ack_recoveries,'lookback_seconds':self.lookback_seconds,'source_requests':self.source.requests,'source_bytes':self.source.bytes,'wall_seconds':time.monotonic()-started,'source_cursor':cursor}
            self.save(result)
            if cursor['cycle'] is None:return result
        result.update(phase='bounded-pause',caught_up=False);self.save(result);return result

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--admin-url',default='http://127.0.0.1:8081');p.add_argument('--status-url',default='http://127.0.0.1:8080/index/status')
    p.add_argument('--admin-secret-env',default='ADMIN_SECRET');p.add_argument('--turso-url-env',default='TURSO_URL');p.add_argument('--turso-token-env',default='TURSO_AUTH_TOKEN')
    p.add_argument('--page-size',type=int,default=500);p.add_argument('--max-pages',type=int,default=256);p.add_argument('--max-cycle-seconds',type=float,default=300)
    p.add_argument('--policy-mode',choices=['off','snapshot'],default='off')
    p.add_argument('--source-mode',choices=['observed','timestamp'],default='observed');p.add_argument('--anti-entropy-pages',type=int,default=2)
    p.add_argument('--sweep-cooldown-seconds',type=int,default=3600);p.add_argument('--max-sweep-seconds',type=float,default=5)
    p.add_argument('--lookback-seconds',type=int,default=300);p.add_argument('--max-source-bytes',type=int,default=512*1024*1024);p.add_argument('--poll-reserve-bytes',type=int);p.add_argument('--http-timeout',type=float,default=15);p.add_argument('--interval',type=float,default=60)
    p.add_argument('--budget-file');p.add_argument('--budget-build-id');p.add_argument('--max-run-seconds',type=float,default=3600);p.add_argument('--status-file');p.add_argument('--once',action='store_true')
    return p

def main():
    args=parser().parse_args()
    if args.source_mode=='timestamp' and args.policy_mode!='off':raise SafeError('policy snapshot requires observed source mode')
    if not 0<args.http_timeout<=60 or not 1<=args.interval<=3600 or not 0<args.max_run_seconds<=24*3600:raise SafeError('invalid HTTP/poll/run bounds')
    secrets=[]
    for name in [args.turso_url_env,args.turso_token_env,args.admin_secret_env]:
        value=os.environ.get(name)
        if not value:raise SafeError('missing required cloud-only credential variable '+name)
        secrets.append(value)
    if bool(args.budget_file)!=bool(args.budget_build_id):raise SafeError('durable source budget requires file and build identity')
    if args.budget_file:
        from budget import accounted_source
        source=accounted_source(sys.modules[__name__],secrets[0],secrets[1],Path(args.budget_file),args.budget_build_id,args.max_source_bytes,poll_reserve_bytes=args.poll_reserve_bytes,window_seconds=3600,min_request_interval=0,http=Http(args.http_timeout))
    else:source=Turso(secrets[0],secrets[1],http=Http(args.http_timeout),max_bytes=args.max_source_bytes,poll_reserve_bytes=args.poll_reserve_bytes)
    admin=Admin(args.admin_url,secrets[2],args.status_url,http=Http(args.http_timeout))
    def expired(*_):raise SafeError('bounded replay process deadline')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,args.max_run_seconds)
    started=time.monotonic()
    while True:
        try:
            settings=dict(page_size=args.page_size,max_pages=args.max_pages,max_seconds=args.max_cycle_seconds,status_file=args.status_file,lookback_seconds=args.lookback_seconds)
            if args.source_mode=='observed':
                from current_state import ObservedSynchronizer
                runner=ObservedSynchronizer(source,admin,sys.modules[__name__],anti_entropy_pages=args.anti_entropy_pages,policy_mode=args.policy_mode,sweep_cooldown_seconds=args.sweep_cooldown_seconds,max_sweep_seconds=args.max_sweep_seconds,**settings)
            else:runner=Synchronizer(source,admin,**settings)
            result=runner.run()
            print(json.dumps({k:v for k,v in result.items() if k!='source_cursor'}),flush=True)
        except SafeError as exc:
            failure={'phase':'replay-error','caught_up':False,'source_mode':args.source_mode,'reason':str(exc),'source_budget':source.accounting()}
            if args.status_file:Synchronizer(source,admin,status_file=args.status_file).save(failure)
            print(json.dumps(failure),file=sys.stderr,flush=True)
            if args.once:raise
        if args.once:return
        if time.monotonic()-started>=args.max_run_seconds:return
        time.sleep(args.interval)
if __name__=='__main__':
    try:main()
    except SafeError as exc:print(json.dumps({'error':'readonly_replay_failed','reason':str(exc)}),file=sys.stderr);sys.exit(1)
    except (ValueError,KeyError,TypeError,OSError):print(json.dumps({'error':'readonly_replay_failed','reason':'request or parsing failure'}),file=sys.stderr);sys.exit(1)

#!/usr/bin/env python3
"""Bounded dev-only real-corpus assertions; never a full top-K oracle or load test."""
import argparse,hashlib,http.client,json,math,os,platform,re,signal,struct,sys,time,urllib.error,urllib.parse,urllib.request
DEV_APP='typeahead-tantivy-dev'
JUSTIN='did:plc:ec64xv7n5dszeizw56yr6b5h'
HANDLE='justinfagnani.com'
MAX_BODY=512*1024
MAX_TOTAL=16*1024*1024
class Failure(Exception):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise Failure('redirect prohibited')

def canonical_host(raw):
 if not isinstance(raw,str):return None
 value=re.sub(r'^https?://','',raw,flags=re.I)
 if value.endswith('/'):value=value[:-1]
 if value.endswith('.'):value=value[:-1]
 if not value or len(value)>253 or '.' not in value:return None
 if any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label) for label in value.split('.')):return None
 return value.lower()

def ascii_normalize(raw,query=False,handle=False):
 # Deliberately restricted independent predicate: do not guess non-ASCII folds.
 if not isinstance(raw,str) or not raw.isascii():return None
 if query:raw=''.join(c for c in raw if c not in '\\%_"' and ord(c)>=32 and ord(c)!=127)
 out=[];pending=False
 for c in raw.lower():
  if c.isalnum() or c in '.-' or (c=='_' and not handle):
   if pending and out:out.append(' ')
   out.append(c);pending=False
  elif c=='_' and handle:continue
  elif c in ' \t\n\r()[]{},;:/\\|!?\"\'`@#$%^&*+=<>~':pending=bool(out)
 return ''.join(out)

def ascii_sound(actor,q):
 value=ascii_normalize(q,query=True)
 if value is None or not value:return None
 handle=ascii_normalize(actor.get('handle',''),handle=True)
 if handle is not None and ' ' not in value:
  if handle.startswith(value) or any(s.startswith(value) for s in ['.'.join(handle.split('.')[i:]) for i in range(1,len(handle.split('.')))]):return True
 display=ascii_normalize(actor.get('displayName',''))
 if display is None:return None
 tokens=display.split()
 if ' ' in value:return all(any(t.startswith(word) for t in tokens) for word in value.split())
 matched=any(t.startswith(value) for t in tokens)
 return True if matched else None if handle is None else False

def is_han(c):return 0x3400<=ord(c)<=0x4dbf or 0x4e00<=ord(c)<=0x9fff or 0xf900<=ord(c)<=0xfaff

def han_sound(actor,q):
 if not q or not all(is_han(c) for c in q):return None
 if not actor.get('handle','').isascii():return None
 # Pure Han substring membership is independent of kana/Latin normalization.
 display=actor.get('displayName','')
 if any(not c.isascii() and not is_han(c) for c in display):return None
 display=''.join(c for c in display if ord(c)>=32 and ord(c)!=127 or c in '\t\n\r')
 runs=re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+',display)
 return any((run.startswith(q) if len(q)==1 else q in run) for run in runs)

def f32(value):
 value=struct.unpack('<f',struct.pack('<f',float(value)))[0]
 if not math.isfinite(value):raise Failure('source score is not finite f32')
 return 0.0 if value==0 else value

def target_url(value):
 u=urllib.parse.urlsplit(value)
 cloud=platform.system()=='Linux' and bool(os.environ.get('FLY_MACHINE_ID'))
 if not cloud:raise Failure('real qualification must run on a Fly Linux VM')
 private=bool(re.fullmatch(r'[0-9a-f]+\.vm\.typeahead-tantivy-dev\.internal',u.hostname or ''))
 local=u.hostname in ['127.0.0.1','localhost','::1'] and os.environ.get('FLY_APP_NAME')==DEV_APP
 if (u.scheme not in ['http','https'] or not (u.hostname==DEV_APP+'.fly.dev' or private or local)
     or u.username or u.password or u.path not in ['','/'] or u.query or u.fragment):raise Failure('target is not the isolated dev backend')
 return value.rstrip('/')

class Http:
 def __init__(self,max_requests=96):self.opener=urllib.request.build_opener(NoRedirect());self.requests=0;self.bytes=0;self.max_requests=max_requests;self.last_request=0;self.source_requests=0
 def get(self,url):return self.request(url)
 def request(self,url,body=None,token=None,body_limit=MAX_BODY):
  self.requests+=1
  if self.requests>self.max_requests:raise Failure('request budget exceeded')
  if self.bytes>=MAX_TOTAL:raise Failure('total response byte budget exceeded')
  time.sleep(max(0,.1-(time.monotonic()-self.last_request)));self.last_request=time.monotonic()
  headers={'User-Agent':'typeahead-tantivy-real-qualification/1'}
  if body is not None:headers['Content-Type']='application/json'
  if token:headers['Authorization']='Bearer '+token
  request=urllib.request.Request(url,headers=headers,data=None if body is None else json.dumps(body,separators=(',',':'),allow_nan=False).encode())
  try:response=self.opener.open(request,timeout=5)
  except urllib.error.HTTPError as error:response=error
  with response:
   payload=bytearray();deadline=time.monotonic()+5
   while True:
    if time.monotonic()>deadline:raise Failure('response elapsed deadline exceeded')
    block=response.read1(min(65536,body_limit+1-len(payload)))
    if not block:break
    payload.extend(block);self.bytes+=len(block)
    if len(payload)>body_limit or self.bytes>MAX_TOTAL:raise Failure('response byte budget exceeded')
   try:value=json.loads(payload)
   except (ValueError,UnicodeError):raise Failure('invalid JSON response') from None
   return response.status,{k.lower():v for k,v in response.headers.items()},value,hashlib.sha256(payload).hexdigest()

def status(http,target):
 code,_,value,_=http.get(target+'/index/status')
 if code!=200 or value.get('engine')!='tantivy' or not value.get('full_source') or not value.get('import_complete') or not value.get('ready'):raise Failure('dev index is not admitted complete and fresh')
 return value

def stamp(value):return {k:value.get(k) for k in ['build_id','source_cursor','watermark','actor_count']}
def search(http,target,q,limit=10,**extra):
 code,headers,body,digest=http.get(target+'/search?'+urllib.parse.urlencode({'q':q,'limit':limit,**extra}))
 if code!=200 or not isinstance(body.get('actors'),list):raise Failure('search did not return successful actors')
 actors=body['actors'];ids=[a.get('did') for a in actors]
 if len(actors)>limit or len(ids)!=len(set(ids)) or any(not isinstance(d,str) or not d.startswith('did:') for d in ids):raise Failure('invalid result identifiers/count')
 return {'q':q,'limit':limit,'params':extra,'actors':actors,'headers':headers,'body_sha256':digest}

def source_rows(http,dids):
 # Fixed primary-key SELECTs only. Credentials stay in the existing cloud env.
 raw=os.environ.get('TURSO_URL','');token=os.environ.get('TURSO_AUTH_TOKEN','')
 if raw.startswith('libsql://'):raw='https://'+raw[len('libsql://'):]
 u=urllib.parse.urlsplit(raw)
 if (u.scheme!='https' or not u.hostname or not u.hostname.endswith(('.turso.io','.libsql.io'))
     or u.username or u.password or u.query or u.fragment or u.path not in ['','/','/v2/pipeline'] or not token or '\n' in token or '\r' in token):raise Failure('existing cloud read-only source configuration missing/invalid')
 url=urllib.parse.urlunsplit((u.scheme,u.netloc,'/v2/pipeline','',''));rows={};count=0
 for start in range(0,len(dids),100):
  batch=dids[start:start+100];count+=1
  http.source_requests+=1
  if http.source_requests>12:raise Failure('source SELECT budget exceeded')
  placeholders=','.join('?'+str(i+1) for i in range(len(batch)))
  sql='SELECT did, handle, pds, hidden, quality_score, updated_at FROM actors WHERE did IN ('+placeholders+')'
  body={'requests':[{'type':'execute','stmt':{'sql':sql,'args':[{'type':'text','value':did} for did in batch],'want_rows':True}},{'type':'close'}]}
  code,_,reply,_=http.request(url,body,token,body_limit=4*1024*1024)
  if code!=200:raise Failure('read-only source lookup failed')
  try:
   result=reply['results'][0]
   if result['type']!='ok' or result['response']['type']!='execute':raise Failure('source SELECT rejected')
   result=result['response']['result'];columns=[c['name'] for c in result['cols']]
   if columns!=['did','handle','pds','hidden','quality_score','updated_at'] or result.get('affected_row_count',0)!=0:raise Failure('source SELECT column/write contract differs')
   if len(result['rows'])>len(batch):raise Failure('source row count exceeds bound')
   for cells in result['rows']:
    values=[]
    for cell in cells:
     kind=cell['type'];value=cell.get('value')
     values.append(None if kind=='null' else int(value) if kind=='integer' else float(value) if kind=='float' else value if kind=='text' else None)
    row=dict(zip(columns,values))
    if row['did'] not in batch or row['did'] in rows or row['hidden'] not in [0,1]:raise Failure('source identity/visibility contract differs')
    rows[row['did']]=row
  except (KeyError,TypeError,ValueError):raise Failure('source SELECT response malformed') from None
 return rows,count

def pds_state(row):
 if not row:return 'source_unavailable'
 if not row.get('pds'):return 'missing'
 return 'canonical' if canonical_host(row['pds']) else 'invalid'

def pds_candidates(rows,observations,watermark):
 candidates=[];seen=set()
 # Prefer already observed hard-scope rows, then preference rows, then globals.
 ordered=sorted(observations,key=lambda item:0 if item['params'].get('pds') else 1 if item['params'].get('prefer_pds') else 2)
 for result in ordered:
  for actor in result['actors']:
   did=actor['did'];row=rows.get(did)
   if did in seen or not row or type(row.get('updated_at')) is not int or row['updated_at']>watermark or row.get('hidden'):continue
   handle=row.get('handle')
   if not isinstance(handle,str) or not handle or not handle.isascii() or len(handle)>253 or handle.endswith('.invalid') or actor.get('handle')!=handle or not canonical_host(row.get('pds')):continue
   seen.add(did);candidates.append((row,result['case']))
 return candidates

def run(args):
 started=time.monotonic();target=target_url(args.url);http=Http();initial=status(http,target);checks=[];observations=[];inconclusive=[];scope_verified=0;scope_pair_verified=0;additional_receipts=[];historical_pds_state='not_requested';observed_pds_counts={};pds_witness_receipt=None;pds_pair_status='not_requested'
 def check(name,condition,**counts):
  checks.append({'case':name,'passed':bool(condition),**counts})
 def observe(name,q,limit=10,**params):
  result=search(http,target,q,limit,**params);result['case']=name;observations.append(result);return result
 witness=observe('known-exact-regression-witness',HANDLE)
 check(witness['case'],bool(witness['actors']) and witness['actors'][0].get('did')==JUSTIN and witness['actors'][0].get('handle','').lower()==HANDLE)
 for name,q in [('joint','Justin Fa'),('joint-reversed','Fa Justin'),('full-name','Justin Fagnani')]:
  result=observe(name,q,100)
  check(name+'-known-witness-presence',JUSTIN in [a['did'] for a in result['actors']],returned=len(result['actors']))
 for name,q in [('broad-a','a'),('latin-eric','eric'),('han-japan','日本'),('han-tokyo','東京')]:observe(name,q,20)
 for name,params in [('hard-pds',{'pds':'selfhosted.social'}),('pds-first',{'prefer_pds':'selfhosted.social','preference':'pds_first'}),('pds-boost',{'prefer_pds':'selfhosted.social','preference':'boost','boost':100})]:observe(name,'a',20,**params)
 sound_verified=sound_unknown=han_verified=0
 for result in observations:
  for actor in result['actors']:
   valid=ascii_sound(actor,result['q'])
   if valid is None:
    valid=han_sound(actor,result['q'])
    if valid is not None:han_verified+=1
   if valid is None:sound_unknown+=1
   else:
    sound_verified+=1
    check('returned-lexical-soundness-'+result['case'],valid)
 check('independent-lexical-predicate-exercised',sound_verified>0,verified=sound_verified,unverified=sound_unknown,han_verified=han_verified)
 if not han_verified:inconclusive.append('no-independent-pure-Han-candidates-to-check')
 # Compare equivalent requests only when the committed view stays stable.
 for name,left,right in [('word-order','Justin Fa','Fa Justin'),('ascii-case','Justin Fa','jUsTiN fA')]:
  stable=False
  for _ in range(3):
   before=status(http,target);a=search(http,target,left,100);b=search(http,target,right,100);after=status(http,target)
   if stamp(before)==stamp(after):
    check('stable-'+name,[r['did'] for r in a['actors']]==[r['did'] for r in b['actors']]);stable=True;break
  if not stable:inconclusive.append('no-stable-generation-window-'+name)
 if args.source_lookups:
  dids=sorted({JUSTIN}|{a['did'] for result in observations for a in result['actors']})
  if len(dids)>1000:raise Failure('source row lookup budget exceeded')
  rows,selects=source_rows(http,dids);covered=skipped=0
  witness_source=rows.get(JUSTIN)
  historical_pds_state=pds_state(witness_source)
  observed_pds_counts={key:sum(pds_state(row)==key for row in rows.values()) for key in ['missing','invalid','canonical']}
  if not witness_source or witness_source.get('hidden') or witness_source.get('handle','').lower()!=HANDLE:
   inconclusive.append('historical-regression-source-witness-unavailable-or-changed')
   for item in checks:
    if 'known-witness-presence' in item['case'] or item['case']=='known-exact-regression-witness':item.update(passed=None,classification='source-witness-unavailable')
  elif type(witness_source.get('updated_at')) is not int or witness_source['updated_at']>initial['watermark']:
   inconclusive.append('historical-regression-source-witness-newer-than-backend-watermark')
   for item in checks:
    if 'known-witness-presence' in item['case'] or item['case']=='known-exact-regression-witness':item.update(passed=None,classification='source-witness-not-covered-yet')
  else:
   for item in checks:
    if 'known-witness-presence' in item['case'] or item['case']=='known-exact-regression-witness':item['classification']='covered-source-witness-regression'

  for result in observations:
   scope=result['params'].get('pds');preferred=result['params'].get('prefer_pds');first=result['params'].get('preference')=='pds_first';boost=f32(result['params'].get('boost',0));rank=[];complete=True
   for actor in result['actors']:
    row=rows.get(actor['did'])
    if not row or type(row.get('updated_at')) is not int or row['updated_at']>initial['watermark']:
     skipped+=1;complete=False;continue
    covered+=1
    check('source-visible-'+result['case'],not row['hidden'] and bool(row['handle']) and not row['handle'].endswith('.invalid'))
    check('source-handle-hydration-'+result['case'],actor.get('handle')==row['handle'])
    host=canonical_host(row['pds'])
    if scope:
     scope_verified+=1
     check('source-pds-membership-'+result['case'],host==scope)
    local=bool(preferred and host==preferred)
    q=ascii_normalize(result['q'],query=True);handle=ascii_normalize(row['handle'],handle=True)
    if q is None or handle is None:complete=False;continue
    rank.append((not(' ' not in q and handle==q),not(first and local),-(f32(row['quality_score'])+(boost if local else 0)),actor['did']))
   if complete:check('source-returned-order-'+result['case'],rank==sorted(rank),verified=len(rank))
  # PDS correctness needs any covered real witness, not metadata on a fixed person.
  candidates=pds_candidates(rows,observations,initial['watermark']);pds_pair_status='no_eligible_source_witness'
  for candidate,origin in candidates[:3]:
   host=canonical_host(candidate['pds']);handle=candidate['handle'];did=candidate['did']
   before=status(http,target)
   global_exact=search(http,target,handle,10)
   if not global_exact['actors'] or global_exact['actors'][0]['did']!=did:
    pds_pair_status='no_verified_global_exact_witness';continue
   outside_host='qualification-empty.invalid' if host!='qualification-empty.invalid' else 'qualification-other.invalid'
   scoped=search(http,target,handle,10,pds=host)
   outside=search(http,target,handle,10,pds=outside_host)
   after=status(http,target)
   if stamp(before)!=stamp(after):
    pds_pair_status='unstable_dev_generation';continue
   refreshed,_=source_rows(http,[did])
   if refreshed.get(did)!=candidate:
    pds_pair_status='source_changed_during_probe';break
   positive=bool(scoped['actors']) and scoped['actors'][0]['did']==did and scoped['headers'].get('x-typeahead-pds')==host
   negative=did not in [actor['did'] for actor in outside['actors']]
   check('source-real-witness-hard-pds-positive',positive)
   check('source-real-witness-hard-pds-exclusion',negative)
   scope_verified+=1;scope_pair_verified=int(positive and negative);pds_pair_status='verified' if scope_pair_verified else 'retrieval_assertion_failed'
   additional_receipts=[{'case':name,'returned':len(reply['actors']),'body_sha256':reply['body_sha256']} for name,reply in [('source-real-witness-global-exact',global_exact),('source-real-witness-hard-pds-positive',scoped),('source-real-witness-hard-pds-exclusion',outside)]]
   pds_witness_receipt={'origin_case':origin,'did_sha256':hashlib.sha256(did.encode()).hexdigest(),'host_sha256':hashlib.sha256(host.encode()).hexdigest(),'positive_and_negative_verified':bool(scope_pair_verified)}
   break
  if not scope_pair_verified and pds_pair_status!='retrieval_assertion_failed':inconclusive.append('pds-pair-'+pds_pair_status)
  check('source-verification-covered-rows',covered>0,covered=covered,source_requests=http.source_requests,unverified=skipped)
  if not scope_verified:inconclusive.append('no-source-backed-hard-pds-positive-witness')
  if skipped:inconclusive.append('source-rows-newer-than-captured-backend-watermark-or-missing')
 final=status(http,target)
 check('generation-identity-preserved',initial['build_id']==final['build_id'] and initial['source_build_id']==final['source_build_id'])
 passed=all(c['passed'] for c in checks) and not inconclusive
 result={'owner':'tantivy-dev-20261028','kind':'bounded-real-corpus-correctness-properties','passed':passed,'source_verified':args.source_lookups,'source_backed_scope_witnesses':scope_verified,'source_backed_pds_pair_verified':bool(scope_pair_verified),'pds_pair_status':pds_pair_status,'historical_witness_pds_state':historical_pds_state,'observed_source_pds_counts':observed_pds_counts,'pds_witness':pds_witness_receipt,
  'full_topk_oracle':False,'checks':checks,'inconclusive':inconclusive,'requests':http.requests,'received_bytes':http.bytes,'wall_seconds':time.monotonic()-started,
  'initial':{k:initial.get(k) for k in ['build_id','source_build_id','actor_count','watermark','pds_missing_actor_count','pds_invalid_actor_count','pds_unknown_actor_count','pds_invalid_reason_counts']},
  'final':{k:final.get(k) for k in ['build_id','source_build_id','actor_count','watermark','pds_missing_actor_count','pds_invalid_actor_count','pds_unknown_actor_count','pds_invalid_reason_counts']},
  'response_receipts':[{'case':r['case'],'returned':len(r['actors']),'body_sha256':r['body_sha256']} for r in observations]+additional_receipts,
  'limitations':['Known witness assertions are regression requirements, not complete expected topK lists.','Independent lexical checks intentionally abstain on non-ASCII folds outside their small ASCII/pure-Han predicate.','Source comparison checks returned rows/order and scope eligibility, not absent candidates or full topK recall.','Source updated_at/commit timestamp guarantees remain a separate replay contract.','No source writes, production requests, admin mutations or raw actor/source payloads in this receipt.']}
 return result

def self_test():
 assert canonical_host('HTTPS://ACME.com./')=='acme.com'
 assert canonical_host('acme.com:443') is None
 assert ascii_sound({'handle':'other.test','displayName':'Justin Fagnani'},'Fa Justin') is True
 assert ascii_sound({'handle':'other.test','displayName':'Justin Other'},'Justin Fa') is False
 assert ascii_sound({'handle':'other.test','displayName':'Éric'},'eric') is None
 assert ascii_sound({'handle':'éric.test','displayName':'Other'},'eric') is None
 assert ascii_sound({'handle':'éric.test','displayName':'Eric'},'eric') is True
 assert han_sound({'displayName':'日本 東京 本太'},'日本太') is False
 assert han_sound({'displayName':'名前日本太郎'},'日本太') is True
 assert han_sound({'displayName':'名前日本太郎'},'本') is False
 assert han_sound({'displayName':'日👋本'},'日本') is None
 assert han_sound({'displayName':'東京・日本'},'日') is None
 assert han_sound({'handle':'日本.test','displayName':'Other'},'日本') is None
 assert han_sound({'displayName':'日\x00本'},'日本') is True
 assert han_sound({'displayName':'日\u4dc0本'},'日本') is None
 assert pds_state({'pds':''})=='missing'
 assert pds_state({'pds':'http://localhost:21'})=='invalid'
 rows={JUSTIN:{'did':JUSTIN,'handle':HANDLE,'pds':'','hidden':0,'updated_at':100},'did:fixture:known':{'did':'did:fixture:known','handle':'known.test','pds':'https://known.test','hidden':0,'updated_at':100}}
 observations=[{'case':'global','params':{},'actors':[{'did':JUSTIN,'handle':HANDLE}]},{'case':'hard-pds','params':{'pds':'known.test'},'actors':[{'did':'did:fixture:known','handle':'known.test'}]}]
 candidates=pds_candidates(rows,observations,100)
 assert len(candidates)==1 and candidates[0][0]['did']=='did:fixture:known' and candidates[0][1]=='hard-pds'
 assert not pds_candidates(rows,observations,99)
 assert f32(16777216)==f32(16777217)
 print(json.dumps({'self_test_passed':True,'network_requests':0}))

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url');p.add_argument('--source-lookups',action='store_true');p.add_argument('--self-test',action='store_true');args=p.parse_args()
 if args.self_test:self_test();return
 if not args.url:p.error('--url is required')
 def expired(*_):raise Failure('qualification hard wall deadline')
 signal.signal(signal.SIGALRM,expired);signal.alarm(180)
 try:result=run(args)
 except (Failure,OSError,ValueError,KeyError,TypeError,OverflowError,struct.error,http.client.HTTPException):
  # Never print requests, credentials, source response bodies or profile payloads.
  error=sys.exc_info()[1]
  print(json.dumps({'passed':False,'kind':'bounded-real-corpus-correctness-properties','error':'qualification_failed','reason':str(error) if isinstance(error,Failure) else type(error).__name__,'full_topk_oracle':False}));sys.exit(1)
 print('TANTIVY_DEV_REAL_QUALIFICATION '+json.dumps(result,separators=(',',':')))
 if not result['passed']:sys.exit(1)
if __name__=='__main__':main()

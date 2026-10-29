"""Bounded private pilot client; tiny-fixture lifecycle evidence, never a speed claim."""
import http.client
import json
import os
import signal
import time
import urllib.parse

def request(origin,path,method='GET',nonce=None):
    url=urllib.parse.urlsplit(origin)
    if url.scheme!='http' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError('explicit private HTTP origin required')
    host=url.hostname
    if host not in {'::1','127.0.0.1'} and not host.endswith('.internal'):raise ValueError('pilot origin must be Fly private or loopback')
    conn=http.client.HTTPConnection(host,url.port or 8080,timeout=3)
    try:
        conn.request(method,path,headers={'X-Pilot-Authorization':nonce} if nonce else {})
        response=conn.getresponse();raw=response.read(524289)
        if len(raw)>524288 or response.status!=200:raise ValueError('pilot HTTP response rejected')
        return json.loads(raw)
    finally:conn.close()

def ready(origin,until,allow_local_fixture=False):
    while time.time()<until:
        try:
            value=request(origin,'/__evaluation')
            if value['ready'] and (value['runtime_process_verified'] or (allow_local_fixture and value.get('synthetic_local_fixture') is True)):return value
        except (OSError,ValueError):pass
        time.sleep(.25)
    raise TimeoutError('pilot readiness deadline')

def exercise(origin,control,nonce,until,expected_arm,allow_local_fixture=False,expected_binary_sha256=None,control_secret=None):
    initial=ready(origin,until,allow_local_fixture)
    if initial['arm']!=expected_arm or initial['nonce']!=nonce or initial['source_ready_attestation'] is not False:
        raise ValueError('pilot target identity differs')
    if expected_binary_sha256 is not None and initial['engine_binary_sha256']!=expected_binary_sha256:
        raise ValueError('pilot binary differs from frozen image receipt')
    def query(q):return [a['did'] for a in request(origin,'/search?'+urllib.parse.urlencode({'q':q,'limit':10}))['actors']]
    assert query('g00.fixture.test')==['did:fixture:00']
    if control_secret is None:raise ValueError('pilot control capability required')
    before=time.monotonic();commit=request(control,'/apply','POST',control_secret)
    assert query('g00.fixture.test')==[]
    assert query('renamed.fixture.test')==['did:fixture:00']
    request(control,'/restart','POST',control_secret);identity=ready(origin,until,allow_local_fixture)
    assert identity['engine_binary_sha256']==initial['engine_binary_sha256']
    assert query('renamed.fixture.test')==['did:fixture:00']
    replay=request(control,'/apply','POST',control_secret)
    assert query('g00.fixture.test')==[]
    assert query('renamed.fixture.test')==['did:fixture:00']
    return {'arm':expected_arm,'passed':True,'identity':initial,'commit':commit,'replay':replay,
            'commit_restart_replay_seconds':time.monotonic()-before,'searches':6,'provider_hardware_attested':False,
            'performance_qualified':False,'full_corpus_qualified':False}

def main():
    deadline=float(os.environ['QUAL_DEADLINE'])
    if not time.time()<deadline<=time.time()+600:raise ValueError('generator lease invalid')
    def expired(*_):raise TimeoutError('generator hard deadline')
    signal.signal(signal.SIGALRM,expired);signal.alarm(max(1,int(deadline-time.time())))
    nonce=os.environ['QUAL_PILOT_NONCE'];results=[]
    for arm in ['native','tantivy']:
        expected=os.environ['QUAL_'+arm.upper()+'_BINARY_SHA256']
        if len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):raise ValueError('frozen binary SHA required')
        results.append(exercise(os.environ['QUAL_'+arm.upper()+'_URL'],os.environ['QUAL_'+arm.upper()+'_CONTROL_URL'],nonce,deadline,arm,expected_binary_sha256=expected,control_secret=os.environ['QUAL_CONTROL_SECRET']))
    assert results[0]['identity']['corpus_sha256']==results[1]['identity']['corpus_sha256']
    assert results[0]['identity']['updates_sha256']==results[1]['identity']['updates_sha256']
    assert results[0]['identity']['machine_id']!=results[1]['identity']['machine_id']
    print('QUALIFICATION_PILOT_RESULT '+json.dumps({'passed':True,'synthetic':True,'arms':results,'production_contact':False},separators=(',',':')),flush=True)

if __name__=='__main__':main()

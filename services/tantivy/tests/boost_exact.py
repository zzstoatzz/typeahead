import hashlib
import json
import math
import os
from pathlib import Path
import random
import secrets
import struct
import subprocess
import tempfile
from fractions import Fraction

from smoke import BIN, actor, query, start, stop, write_seed


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def expected(rows, q, limit, preferred=None, boost=0.0, pds_first=False, scope=None):
    candidates=[row for row in rows if (scope is None or row['pds']=='https://'+scope)]
    def rank(row):
        local=row['pds']=='https://'+str(preferred)
        score=Fraction.from_float(f32(row['quality_score']))
        if local:score+=Fraction.from_float(f32(boost))
        return (row['handle']==q) is False, (not local if pds_first else False), -score, row['did']
    return [row['did'] for row in sorted(candidates,key=rank)[:limit]]


def run():
    tiny=f32(2**-149);maximum=f32(float.fromhex('0x1.fffffep+127'))
    values=[-maximum,-f32(1e30),-2.0,-1.0,-tiny,-0.0,0.0,tiny,1.0,2.0,f32(1e30),maximum]
    rng=random.Random(271828)
    while len(values)<48:
        value=struct.unpack('<f',struct.pack('<I',rng.getrandbits(32)))[0]
        if math.isfinite(value):values.append(value)
    rows=[];groups={}
    for group,name in enumerate(['HugeBoost','FloatRange']):
        scores=[1.0,2.0] if group==0 else values
        current=[]
        for local,host in [(False,'remote.test'),(True,'local.test')]:
            for n,score in enumerate(scores):
                row=actor(1000+group*500+int(local)*100+n,f'g{group}-{int(local)}-{n}.test',name,score,'https://'+host)
                rows.append(row);current.append(row)
        groups[name]=current
    exact=[]
    for n,(handle,score,host) in enumerate([('exact.test',-maximum,'local.test'),('exact.test',1.0,'remote.test'),('exact.test',2.0,'local.test'),('exact.test.long',maximum,'local.test'),('exact.test.longer',maximum,'remote.test')]):
        row=actor(9000+n,handle,'Exact group',score,'https://'+host);rows.append(row);exact.append(row)
    groups['exact.test']=exact
    checks=0
    with tempfile.TemporaryDirectory(prefix='tantivy-exact-boost-') as temporary:
        root=Path(temporary);data=root/'data';seed=root/'seed.jsonl';write_seed(seed,rows)
        imported=subprocess.run([str(BIN),'import','--data',str(data),'--input',str(seed),'--allow-partial','true','--batch-size','17'],capture_output=True,timeout=30)
        assert imported.returncode==0,imported.stderr
        with (root/'server.log').open('wb') as log:
            child,public,admin=start(data,log,secrets.token_urlsafe(24))
            try:
                for q,current in groups.items():
                    for boost in [0.0,tiny,f32(.1),1.0,f32(1e30),maximum]:
                        for scope in [None,'local.test']:
                            params={'prefer_pds':'local.test','preference':'boost','boost':repr(boost)}
                            if scope:params['pds']=scope
                            for limit in [1,2,5,25,100]:
                                result,_=query(public,q,limit=limit,**params)
                                got=[row['did'] for row in result]
                                wanted=expected(current,q,limit,'local.test',boost,scope=scope)
                                assert got==wanted,{'q':q,'limit':limit,'boost':boost,'scope':scope,'expected':wanted,'actual':got}
                                checks+=1
                    for limit in [1,2,25,100]:
                        result,_=query(public,q,limit=limit,prefer_pds='local.test',preference='pds_first')
                        assert [row['did'] for row in result]==expected(current,q,limit,'local.test',pds_first=True)
                        checks+=1
            finally:stop(child)
    receipt={'passed':True,'checks':checks,'oracle':'independent Python Fraction.from_float(f32(base)) + Fraction.from_float(f32(boost))','covers':['huge_boost','subnormals','negative_base','cancellation','maximum_f32','true_equal_sum_DID_ties','exact_priority','hard_PDS','PDS_first','multi_segment','K_prefix_consistency'],'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest(),'production_contact':False}
    if os.environ.get('TA_BOOST_RECEIPT'):Path(os.environ['TA_BOOST_RECEIPT']).write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':run()

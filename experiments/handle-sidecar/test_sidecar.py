import json
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
BIN = ROOT/'target/release/handle-sidecar'


def distance(a, b):
    d = [[0]*(len(b)+1) for _ in range(len(a)+1)]
    for i in range(len(a)+1): d[i][0] = i
    for j in range(len(b)+1): d[0][j] = j
    for i in range(1,len(a)+1):
        for j in range(1,len(b)+1):
            d[i][j] = min(d[i-1][j]+1,d[i][j-1]+1,d[i-1][j-1]+(a[i-1]!=b[j-1]))
            if i>1 and j>1 and a[i-1]==b[j-2] and a[i-2]==b[j-1]:
                d[i][j] = min(d[i][j],d[i-2][j-2]+1)
    return min(d[len(a)])


class Comparison(unittest.TestCase):
    def test_recall_edits_duplicates_scope_and_profile_reads(self):
        rng=random.Random(20261028)
        rows=[(f'abcdefghijklmnop.alpha{i:04}.test',f'did:web:a{i}',10000-i,'big.test') for i in range(1001)]
        rows += [('abcdefghijklmnop.zebra.test','did:web:z',1,'small.test'),('alice.test','did:web:a',3,'big.test'),('alice.test','did:web:b',2,'small.test'),('alice.test.extra','did:web:extra',9999,'big.test')]
        rows += [(''.join(rng.choices('abcdef',k=6))+'.test',f'did:web:r{i}',i%17,'small.test') for i in range(80)]
        cases=[{'id':'long','q':'abcdefghijklmnop.zeb'}, {'id':'transpose','q':'abcdefghijklmnop.zbera','fuzzy':True}, {'id':'delete','q':'alce','fuzzy':True}, {'id':'insert','q':'allice','fuzzy':True}, {'id':'substitute','q':'alixe','fuzzy':True}, {'id':'duplicates','q':'alice'}, {'id':'exact-promotion','q':'alice.test'}, {'id':'scope','q':'abcdefghijklmnop','pds':'small.test'}]
        for i,(h,_,_,_) in enumerate(rows[-80:]):
            q=h[:5]; cases += [{'id':f'e{i}','q':q},{'id':f'f{i}','q':q[:2]+q[3]+q[2]+q[4:],'fuzzy':True}]
        long_query='abcdefghijklmnop.zeb'
        capped=sorted((r for r in rows if r[0].startswith(long_query[:16])),key=lambda r:(-r[2],r[1]))[:100][:50]
        self.assertEqual([r for r in capped if r[0].startswith(long_query)],[])
        self.assertEqual([r[1] for r in rows if r[0].startswith(long_query)],['did:web:z'])
        with tempfile.TemporaryDirectory(prefix='handle-sidecar-fixture-') as t:
            p=Path(t); blob=b'\x01\x00'+struct.pack('<f',1)+b'\x00\x00'*7
            (p/'profiles').write_bytes(blob);(p/'overlay-profiles.bin').write_bytes(blob)
            (p/'sorted.tsv').write_text(''.join(f'{h}\t{did}\t{s}\t{host}\t0\t{len(blob)}\t0\n' for h,did,s,host in sorted(rows)))
            (p/'cases.json').write_text(json.dumps(cases))
            subprocess.run([BIN,'build',p],check=True,capture_output=True)
            outputs=[]
            for variant in ['sorted','fst']:
                run=subprocess.run([BIN,'check',p,variant,p/'profiles',p/'cases.json','20000'],check=True,capture_output=True,text=True)
                outputs.append([json.loads(s) for s in run.stdout.splitlines()])
            self.assertEqual(outputs[0],outputs[1])
            for c,result in zip(cases,outputs[0]):
                scored=[]
                for h,did,score,host in rows:
                    if c.get('pds') and c['pds']!=host:continue
                    dist=distance(c['q'],h) if c.get('fuzzy') else (0 if h.startswith(c['q']) else 2)
                    if dist<=int(c.get('fuzzy',False)):scored.append((h!=c['q'],dist,-score,did,h,score))
                seen=set();expected=[]
                for _,dist,_,did,h,score in sorted(scored):
                    if did not in seen:
                        seen.add(did);expected.append({'did':did,'handle':h,'distance':dist,'score':float(score)})
                self.assertEqual(result['answer']['hits'],expected[:10],c)
                self.assertFalse(result['answer']['truncated'])
            lowcap=subprocess.run([BIN,'check',p,'fst',p/'profiles',p/'cases.json','5'],check=True,capture_output=True,text=True)
            self.assertTrue(any(json.loads(s)['answer']['truncated'] for s in lowcap.stdout.splitlines()))


if __name__=='__main__':unittest.main()

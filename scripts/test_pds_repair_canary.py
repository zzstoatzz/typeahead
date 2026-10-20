import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from pds_repair_canary import baseline_budget


class BaselineTests(unittest.TestCase):
    def test_full_baseline_and_failures(self):
        now=time.time()
        rows=[]
        for i in range(80):
            at=now-(79-i)*15
            rows.append({'at':at,'errors':{},'ingest':{'body':{'status':'ok','role':'ingester','ingest':{'queued':12,'deletes_queued':1,'backpressure_active':False,'backpressure_count':0,'last_write_at':at-2,'source_event_at':at-1},'identity':{'dropped':0,'post_failed':2,'processed':i+100}}},'search':{'body':{'status':'ok','ready':True,'sync_lag_s':i*4}}})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'baseline.jsonl'
            def write(data):path.write_text(''.join(json.dumps(r)+'\n' for r in data))
            rows[40]['ingest']['seconds']=0.7
            rows[40]['search']['seconds']=0.7
            rows[40]['ingest']['body']['ingest']['last_write_at']=rows[40]['at']+0.5
            write(rows);budget,last=baseline_budget(path)
            self.assertEqual(budget['max_queue'],30);self.assertEqual(last['at'],rows[-1]['at'])
            write(rows[:60])
            with self.assertRaises(ValueError):baseline_budget(path)
            for mutate in [lambda r:r['search']['body'].update(sync_lag_s=float('nan')),lambda r:r['ingest']['body']['ingest'].update(backpressure_active=True),lambda r:r['ingest']['body']['identity'].update(post_failed=3)]:
                bad=copy.deepcopy(rows);mutate(bad[40]);write(bad)
                with self.assertRaises(ValueError):baseline_budget(path)


if __name__=='__main__':unittest.main()

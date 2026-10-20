import copy
import time
import unittest
from pds_repair_health import blockers


class HealthTests(unittest.TestCase):
    def setUp(self):
        now=time.time()
        self.sample={'at':now,'errors':{},'ingest':{'body':{'status':'ok','role':'ingester','ingest':{'queued':15,'deletes_queued':2,'backpressure_active':False,'backpressure_count':0,'last_write_at':now-2,'source_event_at':now-1},'identity':{'dropped':0,'post_failed':2,'processed':100}}},'search':{'body':{'status':'ok','ready':True,'sync_lag_s':301}}}
        self.budget=dict(max_queue=100,max_write_age=15,max_search_lag=360)

    def test_normal_poll_cycle_and_existing_counters(self):
        self.assertEqual(blockers(self.sample,self.sample,**self.budget),[])

    def test_fail_closed_on_missing_stale_and_bad_health(self):
        variations=[]
        missing=copy.deepcopy(self.sample);del missing['ingest']['body']['ingest']['last_write_at'];variations.append(missing)
        stale=copy.deepcopy(self.sample);stale['at']-=60;variations.append(stale)
        down=copy.deepcopy(self.sample);down['errors']['search']='TimeoutError';variations.append(down)
        for key,value in [('queued',float('nan')),('queued','15'),('queued',101),('last_write_at',time.time()-20),('backpressure_active',True),('source_event_at',time.time()-20)]:
            row=copy.deepcopy(self.sample);row['ingest']['body']['ingest'][key]=value;variations.append(row)
        for key,value in [('ready',False),('sync_lag_s',361)]:
            row=copy.deepcopy(self.sample);row['search']['body'][key]=value;variations.append(row)
        for row in variations:self.assertTrue(blockers(row,**self.budget))

    def test_counter_increase_and_restart_stop(self):
        for key,value in [('post_failed',3),('dropped',1),('processed',0)]:
            row=copy.deepcopy(self.sample);row['ingest']['body']['identity'][key]=value
            self.assertTrue(blockers(row,self.sample,**self.budget))

    def test_completed_progress_preserves_budget_without_replay_false_alarm(self):
        from pds_fly_progress import EXPECTED
        row=copy.deepcopy(self.sample);now=row['at']
        row['search']['body']['sync_lag_s']=411
        row['search_progress']={'identity':{**EXPECTED,'updated_at':'2026-10-20T15:04:05Z'},
            'source':'fly-machine-completed-sync-log','observed_at':now,'completed_at':now-300}
        self.assertEqual(blockers(row,row,now=now,**self.budget),[])
        for key,value in [('completed_at',now-361),('completed_at',now+1),('observed_at',now-21),('source','unverified')]:
            bad=copy.deepcopy(row);bad['search_progress'][key]=value
            self.assertTrue(blockers(bad,row,now=now,**self.budget))
        self.assertTrue(blockers(self.sample,row,now=now,**self.budget))
        bad=copy.deepcopy(row);bad['search_progress']['identity']['instance_id']='new-boot'
        self.assertTrue(blockers(bad,row,now=now,**self.budget))


if __name__=='__main__':unittest.main()

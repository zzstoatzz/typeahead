import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import suppress

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import budget
import test_sync
sync=test_sync.sync

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.fixture=test_sync.Fixture(self.root);self.ledger=self.root/'budget.json'
    def tearDown(self):self.fixture.close();self.tmp.cleanup()
    def source(self,max_bytes=16000,**kwargs):return budget.accounted_source(sync,self.fixture.url,self.fixture.source_secret,self.ledger,'fixture',max_bytes,allow_test_loopback=True,min_request_interval=0,**kwargs)
    def test_poll_reserve_and_actual_bytes_survive_recreation(self):
        for i in range(64):self.fixture.actor('did:fixture:'+str(i),self.fixture.floor+1)
        source=self.source()
        try:
            with source.budget_scope('sweep'),self.assertRaises(sync.SweepBudgetExhausted):source.select(sync.ACTORS_SQL,[self.fixture.floor,self.fixture.floor,'',self.fixture.now,500])
            self.assertEqual(source.sweep_bytes,12000)
        finally:source.close()
        source=self.source()
        try:
            self.assertEqual(source.now(),self.fixture.now);self.assertEqual(source.sweep_bytes,12000)
            self.assertEqual(source.bytes,source.poll_bytes+source.sweep_bytes);self.assertLessEqual(source.bytes,16000)
            self.assertFalse(json.loads(self.ledger.read_bytes())['pending_reservation'])
        finally:source.close()
    def test_sigkill_retains_only_one_bounded_request_reservation(self):
        released=threading.Event();original=self.fixture.server.RequestHandlerClass.respond
        def delayed(handler,code,value):
            released.wait(10)
            with suppress(OSError):original(handler,code,value)
        self.fixture.server.RequestHandlerClass.respond=delayed
        code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import sync,budget;s=budget.accounted_source(sync,sys.argv[2],sys.argv[3],Path(sys.argv[4]),'fixture',16777216,allow_test_loopback=True,window_seconds=3600,min_request_interval=0);s.now()"
        child=subprocess.Popen([sys.executable,'-c',code,str(HERE),self.fixture.url,self.fixture.source_secret,str(self.ledger)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            until=time.monotonic()+5
            while not self.fixture.requests and time.monotonic()<until:time.sleep(.01)
            self.assertTrue(self.fixture.requests);state=json.loads(self.ledger.read_bytes())
            self.assertTrue(state['pending_reservation']);self.assertEqual(state['reservation_bytes'],4*1024**2+1)
            child.kill();child.wait(5)
        finally:
            released.set()
            if child.poll() is None:child.kill();child.wait(5)
        source=self.source(16777216,window_seconds=3600)
        try:
            self.assertEqual(source.bytes,state['source_bytes']);self.assertEqual(source.accounting()['recovered_reservation_bytes'],4*1024**2+1)
            self.assertEqual(source.now(),self.fixture.now);self.assertGreater(source.bytes,state['source_bytes']);self.assertLess(source.bytes,16777216)
        finally:source.close()
    def test_fixed_window_does_not_renew_on_restart_and_rolls_only_after_deadline(self):
        source=self.source(window_seconds=1)
        source.now();first=source.accounting();source.close()
        recovered=self.source(window_seconds=1)
        try:
            self.assertEqual(recovered.bytes,first['source_bytes']);recovered.now();self.assertGreater(recovered.bytes,first['source_bytes'])
            self.assertEqual(recovered.accounting()['window_started_at'],first['window_started_at'])
            time.sleep(max(0,first['window_ends_at']-time.time()+.02))
            recovered.now();after=recovered.accounting();self.assertGreater(after['window_started_at'],first['window_started_at']);self.assertEqual(after['source_bytes'],first['source_bytes'])
        finally:recovered.close()
    def test_identity_corruption_and_concurrent_owner_fail_closed(self):
        source=self.source()
        try:
            with self.assertRaises(BlockingIOError):self.source()
        finally:source.close()
        state=json.loads(self.ledger.read_bytes());state['build_id']='another';self.ledger.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,'identity changed'):self.source()
        self.assertFalse(self.fixture.requests)

if __name__=='__main__':unittest.main()

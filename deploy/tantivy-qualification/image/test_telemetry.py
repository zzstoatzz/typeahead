"""Real Linux proc/cgroup and process/file lifecycle tests; no fixtures replace metrics."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('qualification_telemetry',HERE/'telemetry.py')
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)

@unittest.skipUnless(sys.platform=='linux','actual Linux proc/cgroup required')
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.children=[]
    def tearDown(self):
        for child in self.children:
            if child.poll() is None:child.terminate()
            child.wait(timeout=3)
            if child.stdout:child.stdout.close()
        self.temp.cleanup()
    def child(self,code):
        child=subprocess.Popen([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,text=True)
        self.children.append(child);return child
    def test_actual_write_memory_cpu_and_process_exit(self):
        target=self.root/'io.data'
        code="""import json,os,sys,time
payload=bytearray(8*1024*1024)
print('ready',flush=True)
time.sleep(.2)
with open(sys.argv[1],'wb') as stream:
 for _ in range(4):stream.write(payload)
 stream.flush();os.fsync(stream.fileno())
print('written',flush=True)
time.sleep(5)
"""
        child=subprocess.Popen([sys.executable,'-u','-c',code,str(target)],stdout=subprocess.PIPE,text=True);self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(),'ready')
        sampler=t.Sampler(index_paths=[self.root]);before=sampler.snapshot([child.pid],False)
        self.assertIsNone(before['actual_interval_seconds'])
        self.assertEqual(child.stdout.readline().strip(),'written')
        after=sampler.snapshot([child.pid],False)
        process=after['processes'][0];old=before['processes'][0]
        self.assertTrue(process['present']);self.assertEqual(old['start_ticks'],process['start_ticks'])
        self.assertGreater(process['memory_bytes']['RssAnon'],0);self.assertGreater(process['memory_bytes']['VmRSS'],0)
        self.assertGreater(process['mapped']['virtual_bytes'],0)
        self.assertEqual(after['summed_process_rss_bytes'],process['memory_bytes']['VmRSS'])
        # wchar is bytes supplied to write(2); write_bytes measures storage I/O.
        self.assertGreaterEqual(process['io_bytes']['wchar']-old['io_bytes']['wchar'],32*1024*1024)
        self.assertGreater(process['io_bytes']['write_bytes']-old['io_bytes']['write_bytes'],0)
        self.assertGreater(after['actual_interval_seconds'],0)
        self.assertGreater(after['uptime_seconds'],0)
        self.assertGreater(after['guest_meminfo_bytes']['MemAvailable'],0)
        self.assertIn('steal',after['guest_cpu_ticks'])
        self.assertIsNotNone(after['guest_vmstat']['pgmajfault'])
        self.assertIsInstance(after['guest_diskstats'],list)
        if Path('/sys/fs/cgroup/memory.current').is_file():
            self.assertGreater(after['cgroup']['memory_current_bytes'],0)
            self.assertIsInstance(after['cgroup']['cpu_stat'],dict)
        self.assertTrue(after['summed_rss_may_double_count_shared_pages'])
        self.assertEqual(before['directories']['observed_monotonic_seconds'],after['directories']['observed_monotonic_seconds'])
        child.terminate();child.wait(timeout=3)
        ended=sampler.snapshot([child.pid],False)['processes'][0]
        self.assertFalse(ended['present']);self.assertIsNone(ended['memory_bytes']);self.assertIsNone(ended['io_bytes'])
    def test_descendants_follow_actual_child_links_without_global_scan(self):
        parent=self.child("import subprocess,sys,time;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)']);print(child.pid,flush=True);child.wait()")
        grandchild=int(parent.stdout.readline())
        snapshot=t.Sampler().snapshot([parent.pid],True)
        self.assertTrue(snapshot['process_selection_complete'])
        self.assertEqual({row['pid'] for row in snapshot['processes']},{parent.pid,grandchild})
        parent.wait(timeout=4)
    def test_bounded_directory_sampling_cache_and_missing_metrics(self):
        index=self.root/'index';index.mkdir()
        (index/('a'*32+'.store')).write_bytes(b'a'*123)
        (index/('a'*32+'.fast')).write_bytes(b'b'*70)
        (index/'merge.temp').write_bytes(b'c'*11)
        value=t.Sampler(index_paths=[index]).snapshot([])['directories']['paths'][0]
        self.assertTrue(value['bounded_complete']);self.assertEqual(value['bytes'],204)
        self.assertEqual(value['segment_groups'],1);self.assertEqual(value['merge_temporary_files'],1)
        capped=t.Sampler(index_paths=[index],file_limit=2).snapshot([])['directories']['paths'][0]
        self.assertFalse(capped['bounded_complete']);self.assertEqual(capped['status'],'entry_limit');self.assertIsNone(capped['bytes'])
        missing=t.Sampler(index_paths=[self.root/'missing']).snapshot([])
        self.assertIsNone(missing['summed_process_rss_bytes'])
        self.assertIsNone(missing['directories']['paths'][0]['bytes'])
        self.assertIsNone(t.read(self.root/'missing'))
        self.assertIsNone(t.pairs(None));self.assertIsNone(t.pressure(self.root/'missing'))
        link=self.root/'link';link.symlink_to(index,target_is_directory=True)
        self.assertEqual(t.Sampler(index_paths=[link]).snapshot([])['directories']['paths'][0]['status'],'symlink_rejected')
        json.dumps(missing,allow_nan=False)
    def test_input_bounds_reject_before_collection(self):
        with self.assertRaises(ValueError):t.Sampler(file_limit=2001)
        with self.assertRaises(ValueError):t.Sampler(directory_interval=1)
        with self.assertRaises(ValueError):t.Sampler(index_paths=['relative'])
        with self.assertRaises(ValueError):t.Sampler().snapshot(list(range(1,34)))
        with self.assertRaises(ValueError):t.Sampler().snapshot([True])

if __name__=='__main__':unittest.main()

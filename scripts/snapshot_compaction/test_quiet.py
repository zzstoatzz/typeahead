import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("TEST_QUIET_HOST") == "1", "requires a Linux host with CPU temperature sensors")
class Tests(unittest.TestCase):
    launcher = Path(__file__).with_name("quiet.py")

    def test_actual_cpu_duty_and_exit_status(self):
        code = """
import json, os, time
start = time.process_time()
wall = time.monotonic()
while time.process_time() - start < 0.4:
    sum(range(1000))
print(json.dumps(dict(cpu=time.process_time()-start, wall=time.monotonic()-wall,
                     affinity=list(os.sched_getaffinity(0)))))
"""
        result = subprocess.run([sys.executable, str(self.launcher), "--", sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
        measured = json.loads(result.stdout)
        self.assertLess(measured["cpu"] / measured["wall"], 0.28)
        self.assertEqual(len(measured["affinity"]), 1)
        failure = subprocess.run([sys.executable, str(self.launcher), "--", sys.executable, "-c", "raise SystemExit(7)"], capture_output=True, timeout=120)
        self.assertEqual(failure.returncode, 7)

    def test_hot_start_never_launches_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "started"
            command = [sys.executable, str(self.launcher), "--max-temp", "1", "--cool-temp", "0.5", "--", sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)]
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                time.sleep(0.5)
                self.assertIsNone(proc.poll())
                self.assertFalse(marker.exists())
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=5)
                self.assertFalse(marker.exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()

    def test_supervisor_death_kills_a_stopped_child(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "pid"
            code = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
            proc = subprocess.Popen([sys.executable, str(self.launcher), "--", sys.executable, "-c", code, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 120
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                proc.kill()
                proc.communicate(timeout=5)
                status = Path(f"/proc/{pid}/stat")
                deadline = time.monotonic() + 5
                while status.exists() and status.read_text().split(") ", 1)[1][0] != "Z" and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(not status.exists() or status.read_text().split(") ", 1)[1][0] == "Z")
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()


if __name__ == "__main__":
    unittest.main()

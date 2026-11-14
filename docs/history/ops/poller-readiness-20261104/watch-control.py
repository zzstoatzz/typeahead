import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent
started = time.time()
until = time.monotonic() + 600
while time.monotonic() < until:
    rows = [json.loads(line) for line in (root / "observations.jsonl").read_text().splitlines()]
    a = [r for r in rows if r["target"] == "a"][-1]
    b = [r for r in rows if r["target"] == "b"][-1]
    if a["at"] > started and a.get("data",{}).get("ready") is False and b.get("data",{}).get("ready") is True:
        subprocess.run([sys.executable, str(root / "public-check.py"), "812056a97999d8",
                        str(root / "public-during-control-failure.json")], check=True, timeout=40)
        (root / "control-failure-witness.json").write_text(json.dumps({"old_a":a,"live_b":b},indent=2)+"\n")
        print("Verified both public aliases and PDS scope on B while old A was unready")
        break
    time.sleep(1)
else:
    print("No control failure observed within the bounded window")

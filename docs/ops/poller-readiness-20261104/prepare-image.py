import subprocess
import sys
from pathlib import Path

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=False)
base = "c476fc591e191a755d0d4837eb4f230932f8ceff"
source = "7d20aa1a099a2e462c6100f8efe72a4885b75475"
slot = subprocess.check_output(["git", "show", base + ":scripts/snapshot_refresh/tantivy-slot.py"])
old = b"'--max-run-seconds','3600'"
assert slot.count(old) == 1
(output / "tantivy-slot.py").write_bytes(slot.replace(old, b"'--max-run-seconds','0'"))
(output / "sync.py").write_bytes(subprocess.check_output([
    "git", "show", source + ":services/tantivy-input/sync.py"]))
(output / "Dockerfile").write_bytes(Path(__file__).with_name("Dockerfile").read_bytes())

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import zlib


with tempfile.TemporaryDirectory(prefix='typeahead-comparison-') as scratch:
    output = Path(scratch)/'result.json'
    command = [sys.executable,str(Path(__file__).with_name('compare.py')),*sys.argv[1:],
        '--output',str(output)]
    child = subprocess.Popen(command,start_new_session=True)
    try:
        code = child.wait(timeout=315)
    except subprocess.TimeoutExpired:
        code = 124
    finally:
        try:
            os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)
    if output.exists():
        with output.open('rb') as stream:
            raw = stream.read(1024**2+1)
        if not 0<len(raw)<=1024**2:
            raise SystemExit('Comparison result exceeds its evidence bound')
        packed = base64.b64encode(zlib.compress(raw)).decode()
        chunks = [packed[i:i+1500] for i in range(0,len(packed),1500)]
        for index,data in enumerate(chunks):
            print('TA_COMPARE_RESULT '+json.dumps(dict(part=index,total=len(chunks),
                sha256=hashlib.sha256(raw).hexdigest(),data=data)),flush=True)
        print('TA_COMPARE_EXIT '+str(code),flush=True)
    raise SystemExit(code)

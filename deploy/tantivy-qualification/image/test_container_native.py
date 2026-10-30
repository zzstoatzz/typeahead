import json
from pathlib import Path
import subprocess
HERE=Path(__file__).resolve().parent
result=subprocess.run(['docker','run','--rm','-i','--network','none','--platform','linux/amd64','--entrypoint','timeout',
                       'typeahead-qualification:native-local','150','python3','-'],input=(HERE/'container_native_pilot.py').read_text(),
                      capture_output=True,text=True,timeout=180)
if result.returncode:raise RuntimeError(result.stdout[-2000:]+result.stderr[-3000:])
line=next(line for line in result.stdout.splitlines() if line.startswith('QUALIFICATION_CONTAINER_NATIVE '))
report=json.loads(line.removeprefix('QUALIFICATION_CONTAINER_NATIVE '));(HERE/'container-native-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

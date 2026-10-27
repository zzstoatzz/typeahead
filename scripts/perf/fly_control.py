import base64
import json
from pathlib import Path
import sys

root = Path('/data/evidence')
if sys.argv[1] == 'status':
    path = root / 'status.json'
    result = json.loads(path.read_text()) if path.exists() else {'phase': 'starting'}
    for name, candidate in [('installation', Path('/data/refresh/install-status.json')), ('measurement', root / 'state.json')]:
        if candidate.is_file(): result[name] = json.loads(candidate.read_text()).get('phase')
    print(json.dumps(result))
elif sys.argv[1] == 'read':
    offset = int(sys.argv[2])
    if not 0 <= offset <= 16 * 1024 * 1024: raise ValueError('invalid evidence offset')
    with (root / 'results.tgz').open('rb') as source:
        source.seek(offset)
        print(base64.b64encode(source.read(24576)).decode())
elif sys.argv[1] == 'collected':
    (root / 'collected').touch(exist_ok=False)
else:
    raise ValueError('invalid evidence operation')

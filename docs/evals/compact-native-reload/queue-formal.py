import json,subprocess,time
from pathlib import Path
root=Path('/home/stoat/typeahead-release-20261017')
for _ in range(120):
    state=subprocess.run(['systemctl','--user','show','typeahead-native-reload-diagnostic-20261017.service','-p','ActiveState','--value'],capture_output=True,text=True,check=True).stdout.strip()
    if state not in ('active','activating'):
        result=json.loads((root/'reload-release/diagnostic-bounded/reload-results.json').read_text())
        assert result['complete'] and not result['formal']
        assert len(result['runs'])==3
        assert {r['role'] for r in result['runs']}=={'baseline','baseline-control','compact'}
        assert all(r['all_bodies_match'] and r['non200']==0 and r['reload_completed_within_fixed_window'] for r in result['runs'])
        assert not (root/'reload-release/formal-bounded').exists()
        subprocess.run(['systemd-run','--user','--unit=typeahead-native-reload-formal-20261017','--property=MemoryMax=2G','--property=MemorySwapMax=0','--property=RuntimeMaxSec=21600','/usr/bin/unshare','--user','--map-root-user','--mount','--fork','sh','-c','mount -t tmpfs -o size=64m tmpfs /tmp && exec python3 /home/stoat/typeahead-release-20261017/run_native_reload_formal.py'],check=True)
        break
    time.sleep(30)
else: raise RuntimeError('Diagnostic remains active; do not restart it')

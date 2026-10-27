"""Qualify only an independent fork of an admitted inactive slot's actual state."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import tarfile
import time

from fly_trial import FlyTrial


def main():
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    evidence = Path('/data/evidence')
    evidence.mkdir(exist_ok=False)
    started = time.monotonic()
    trial = None
    try:
        payload = Path('/data/refresh/index/generations') / os.environ['REFRESH_BUILD_ID']
        manifest = json.loads((payload / 'manifest.json').read_text())
        trial = FlyTrial(manifest)
        trial.args.frozen_overlay = True
        with closing(sqlite3.connect('/data/refresh/overlay.db')) as source, closing(sqlite3.connect(payload / 'frozen-overlay.db')) as frozen:
            source.backup(frozen)
            if frozen.execute('PRAGMA quick_check').fetchone() != ('ok',): raise RuntimeError('forked overlay integrity')
            rows = frozen.execute('SELECT did,blob,updated_at FROM actor_overlay WHERE blob IS NOT NULL AND source=0 ORDER BY did LIMIT 5000').fetchall()
            if len(rows) < 500: raise RuntimeError('insufficient actual projection rows')
            trial.report['overlay_counts'] = {name: frozen.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in ['actor_overlay', 'prefix_overlay']}
        trial.args.actor_count = len(rows)
        (payload / 'feed.json').write_text(json.dumps([{'did': did, 'blob_hex': blob.hex(), 'updated_at': timestamp} for did, blob, timestamp in rows], separators=(',', ':')))
        trial.report.update(capture_method='actual private fork overlay; deterministic first 5000 non-tombstone source-zero actors', limitations=['current actual state only; no aged-capacity claim', 'no live Turso or freshness qualification'])
        trial.measure(manifest)
    except BaseException as exc:
        (evidence / 'failure.json').write_text(json.dumps({'kind': type(exc).__name__, 'message': str(exc)[:1024]}) + '\n')
    finally:
        if trial:
            for child in reversed(trial.children): trial.stop(child)
            try: trial.retire()
            except BaseException as exc:
                (evidence / 'cleanup-failure.json').write_text(json.dumps({'message': str(exc)[:1024], 'requires_volume_retirement': True}))
        with tarfile.open(evidence / 'results.tgz', 'w:gz') as archive:
            for path in evidence.iterdir():
                if path.name not in ('results.tgz', 'status.json') and path.is_file(): archive.add(path, arcname=path.name)
        data = evidence / 'results.tgz'
        (evidence / 'status.json').write_text(json.dumps({'phase': 'results-ready', 'bytes': data.stat().st_size, 'sha256': hashlib.sha256(data.read_bytes()).hexdigest(), 'run_id': os.environ['PERF_RUN_ID']}))
    while time.monotonic() - started < 5100:
        if (evidence / 'collected').exists(): return
        time.sleep(1)


if __name__ == '__main__': main()

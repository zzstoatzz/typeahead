"""Qualify only an independent fork of an admitted inactive slot's actual state."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import urllib.parse
import qualify
import sqlite3
import tarfile
import time

from fly_trial import FlyTrial


def diagnose(trial):
    trial.report['kind'] = 'private-fly-cold-phase-diagnostic'
    trial.report['diagnostics'] = []
    for index, query in enumerate(['nate', 'a', 'photographer']):
        overlay = trial.payload / 'diagnostic-overlay.db'
        shutil.copyfile(trial.payload / 'frozen-overlay.db', overlay)
        receipt = trial.evict_private(overlay)
        trial.save('cold-diagnostic-' + str(index))
        child, base, _, _ = trial.start_server('candidate', overlay, '0' * 64, trial.payload / 'never-start')
        before = trial.metrics(child)
        result, body = qualify.request(base, '/search?q=' + urllib.parse.quote(query) + '&limit=10', 10)
        trial.report['diagnostics'].append({'variant': 'production-default', 'query': query, 'pre_start_residency': receipt,
            'http': result, 'body': json.loads(body), 'before': before, 'after': trial.metrics(child)})
        trial.stop(child)
        (trial.evidence / 'candidate-server.log').rename(trial.evidence / ('diagnostic-' + str(index) + '-server.log'))
        trial.retire_overlay(overlay)
    trial.save('cold-diagnostics-complete')


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
        if os.environ.get("TYPEAHEAD_EVAL_DIAG_ONLY") == "1": diagnose(trial)
        else: trial.measure(manifest)
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

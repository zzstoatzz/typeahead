#!/usr/bin/env python3
"""Exercise the unchanged native builder and loopback server using synthetic data only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[2]
BUDGET = 512 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--membership-cap', type=int)
    args = parser.parse_args()
    if args.membership_cap is not None and not 1 <= args.membership_cap <= 65535:
        parser.error('membership cap must fit the native u16 posting format')
    out = args.output.resolve()
    out.mkdir(exist_ok=False)
    report = {'kind': 'synthetic-native-recall-scope', 'production_access': False,
              'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
              'cases': [], 'owner': 'display-name-recall-20261028', 'scratch_budget_bytes': BUDGET,
              'membership_cap_override':args.membership_cap}
    children = []
    started = time.monotonic()
    scratch = Path(tempfile.mkdtemp(prefix='typeahead-recall-scope-')).resolve()
    report['scratch'] = str(scratch)
    def save():
        (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    def guard():
        if time.monotonic() - started > 900:
            raise TimeoutError('fixture wall budget')
        if sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file()) > BUDGET:
            raise RuntimeError('fixture disk budget')
        if shutil.disk_usage(scratch).free < 1024**3:
            raise RuntimeError('fixture disk reserve')
    def run(cmd, env, log, timeout=300):
        with (out / log).open('w') as stream:
            p = subprocess.Popen(cmd, cwd=REPO/'services', env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            children.append(p)
            deadline = time.monotonic() + timeout
            while p.poll() is None:
                guard()
                if time.monotonic() > deadline:
                    raise TimeoutError(log)
                time.sleep(.2)
            if p.returncode:
                raise RuntimeError(log + ' failed')
    def stop(p):
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=4)
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'], 'LANG': 'C.UTF-8'}
    try:
        install = scratch/'install'
        run(['zig', 'build', '-Doptimize=ReleaseSafe', '--prefix', str(install)], env, 'build.log')
        run(['zig', 'build', 'eval-server', '-Doptimize=ReleaseSafe', '--prefix', str(install)], env, 'build-evaluator.log')
        report['binaries'] = {p.name: hashlib.file_digest(p.open('rb'), 'sha256').hexdigest() for p in (install/'bin').iterdir()}
        rows = []
        source_stamp = int(time.time())-60
        def actor(did, display, score=1, handle=None, pds='scope.test'):
            did = 'did:plc:' + did
            rows.append((did, handle or did.split(':')[-1]+'.fixture.test', display, '', '[]', 'https://'+pds, '', '{}', 0, score, source_stamp))
            return did
        target = actor('justintarget', 'Justin Fagnani', 429, 'justinfagnani.com')
        display_only = actor('displayonlytarget', 'Justin Falconer', 428, 'separate.fixture.test')
        for i in range(100):
            actor(f'justin{i:04}', f'Justin Other{i}', 1000+i)
        for i in range(301):
            actor(f'fa{i:04}', f'Other{i} Falcon{i}', 2000+i)
        veil = [actor(f'veil{i:04}', 'Veil Unique', 3000+i) for i in range(50)]
        visible = actor('veilsurvivor', 'Veil Unique Survivor')
        churn = [actor(f'churn{i:04}', f'Churn Other{i}', 4000+i) for i in range(201)]
        churn_target = actor('churnsurvivor', 'Churn Survivor')
        for i in range(101):
            actor(f'longcap{i:04}', 'Handle Decoy', 5000+i, f'abcdefghijklmnopother{i}.test')
        long_cap = actor('longcaptarget', 'Handle Target', 1, 'abcdefghijklmnopneedle.test')
        for i in range(60):
            actor(f'longhydrate{i:04}', 'Hydration Decoy', 6000+i, f'qrstuvwxyzabcdefother{i}.test')
        long_hydrate = actor('longhydratetarget', 'Hydration Target', 1, 'qrstuvwxyzabcdefneedle.test')
        for i in range(60):
            actor(f'displaylong{i:04}', f'longdisplaytokenother{i}', 7000+i)
        display_long = actor('displaylongtarget', 'longdisplaytokenneedle')
        cjk = actor('cjkwhole', '日本太郎', 90)
        actor('cjkfirst', '日本花子', 80)
        actor('cjkmiddle', '本太', 70)
        actor('cjklast', '太郎', 60)
        mixed = actor('mixedwhole', 'Alice 日本', 50)
        actor('mixedlatin', 'Alice', 40)
        actor('mixedjapan', '日本', 30)
        eight = actor('eighttokens', 'one two three four five six seven eight')
        ninth = actor('ninthtoken', 'red orange yellow green blue indigo violet silver ninth', handle='tokenoverflow.fixture.test')
        for i in range(101):
            actor(f'dupe{i:04}', 'Duplicate', 8000+i, 'duplicate.fixture.test')
        source = scratch/'source.db'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
        report['actor_count'] = len(rows)
        (out/'fixture-actors.json').write_text(json.dumps([{'did':r[0],'handle':r[1],'display':r[2],'pds':r[5],'hidden':bool(r[8]),'score':r[9]} for r in rows], indent=2)+'\n')
        build_env = env | {'MODE':'indexer', 'INDEX_CHANNEL':'local', 'INDEX_SOURCE':'export', 'INDEX_SAMPLE_LIMIT':'0', 'INDEX_PDS_MAX_ACTORS':'10000', 'INDEX_REUSE_SOURCE':str(source), 'INDEX_BUILD_ROOT':str(scratch/'build'), 'INDEXER_VERSION':report['source']}
        if args.membership_cap is not None:
            build_env |= {key:str(args.membership_cap) for key in ['INDEX_TOP_N_SHORT','INDEX_TOP_N_MEDIUM','INDEX_TOP_N_LONG']}
        run([str(install/'bin/typeahead-ingester')], build_env, 'fixture-build.log')
        snapshot = next((scratch/'build').glob('build-*/index.db'))
        with sqlite3.connect(snapshot) as db:
            report['snapshot_meta'] = dict(db.execute('SELECT key,value FROM index_meta'))
            blobs = dict(db.execute('SELECT did,blob FROM actor_compact_base'))
            postings = [r[0] for r in db.execute('SELECT dids FROM prefix_top_base')]
            report['posting_storage'] = {'retained_memberships':sum(int.from_bytes(b[1:3],'little') for b in postings), 'packed_bytes':sum(map(len,postings)), 'keys':len(postings)}
        update_stamp = int(report['snapshot_meta']['source_watermark'])+1
        report['snapshot_bytes'] = snapshot.stat().st_size
        overlay = scratch/'overlay.db'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        server_env = env | {'TYPEAHEAD_EVAL_NONCE':'recall-scope-local-fixture', 'TYPEAHEAD_EVAL_SNAPSHOT':str(snapshot), 'LOCAL_DB_PATH':str(overlay), 'SQLITE_TMPDIR':str(scratch/'tmp'), 'PORT':str(port)}
        log = (out/'server.log').open('w')
        p = subprocess.Popen([str(install/'bin/eval-server')], cwd=REPO/'services', env=server_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        children.append(p)
        def request(path, params):
            with urllib.request.urlopen(base+path+'?'+urllib.parse.urlencode(params), timeout=3) as r:
                return r.status, json.load(r)
        deadline = time.monotonic()+20
        while True:
            try:
                status, identity = request('/__evaluation', {})
                if identity.get('nonce') != 'recall-scope-local-fixture':
                    raise RuntimeError('wrong fixture server')
                break
            except OSError:
                if p.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('fixture server not ready')
                time.sleep(.1)
        def case(name, query, expected, pds=None, group='candidate-recall'):
            guard()
            params = {'q':query,'limit':10}
            if pds: params['pds']=pds
            status, body = request('/search', params)
            _, trace = request('/debug/search', params)
            actual = [a['did'] for a in body['actors']]
            expected = sorted(expected)
            report['cases'].append({'id':name,'group':group,'query':query,'pds':pds,'expected_dids':expected,'actual_dids':actual,'matches_oracle':set(actual)==set(expected),'status':status,'trace':trace})
            save()
        case('joint-broad','Justin Fa',[target,display_only])
        case('joint-reversed','Fa Justin',[target,display_only])
        case('joint-narrow','Justin Fag',[target])
        case('joint-complete','Justin Fagnani',[target])
        case('single-selective','fagnani',[target])
        case('display-only-complete','Justin Falconer',[display_only])
        case('scoped-joint-broad','Justin Fa',[target,display_only],pds='scope.test')
        case('scoped-joint-narrow','Justin Fag',[target],pds='scope.test')
        case('unknown-scope','Justin Fa',[],pds='unknown.test',group='scope-control')
        case('long-handle-builder-cap','abcdefghijklmnopneed',[long_cap])
        case('long-handle-hydration-cap','qrstuvwxyzabcdefneed',[long_hydrate])
        case('long-handle-exact-promotion','qrstuvwxyzabcdefneedle.test',[long_hydrate])
        case('long-display-hydration-cap','longdisplaytokenneed',[display_long])
        case('cjk-single-token','日本太郎',[cjk],group='query-semantics')
        case('cjk-spaced','日本 太郎',[cjk],group='query-semantics')
        case('mixed-script-single-token','alice日本',[mixed],group='query-semantics')
        case('mixed-script-spaced','alice 日本',[mixed],group='query-semantics')
        case('query-nine-tokens','one two three four five six seven eight nine',[],group='query-limit')
        case('display-ninth-token','ninth',[ninth],group='documented-index-limit')
        _, duplicate = request('/debug/search', {'q':'duplicate.fixture.test','limit':100})
        report['exact_duplicate_trace'] = {'merged':duplicate['merged'],'candidates':len(duplicate['candidates']),'hydrate_cap':duplicate['hydrate_cap']}
        with sqlite3.connect(overlay) as db:
            for did in veil:
                db.execute('INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES(?,?,1,?,0)',(did,blobs[did],update_stamp))
            for did in churn:
                db.execute('INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES(?,NULL,0,?,0)',(did,update_stamp))
        case('hidden-top50','veil unique',[visible],group='post-cap-eligibility')
        case('hidden-top50-scoped','veil unique',[visible],pds='scope.test',group='post-cap-eligibility')
        case('deleted-base-top200','churn',[churn_target],group='base-cap-eligibility')
        stop(p)
        log.close()
        feed = scratch/'feed.json'
        feed.write_text(json.dumps([{'did':did,'blob_hex':blobs[did].hex(),'updated_at':update_stamp} for did in [target,display_only]]))
        server_env |= {'TYPEAHEAD_EVAL_FEED':str(feed), 'TYPEAHEAD_EVAL_MAX_ACTORS':'2', 'TYPEAHEAD_EVAL_WRITER_MODE':'replay', 'TYPEAHEAD_EVAL_WRITER_START':str(scratch/'never-start'), 'TYPEAHEAD_EVAL_BACKGROUND_SLOTS':'1'}
        def restart_server(label):
            stream = (out/(label+'.log')).open('w')
            child = subprocess.Popen([str(install/'bin/eval-server')], cwd=REPO/'services', env=server_env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            children.append(child)
            limit = time.monotonic()+20
            while True:
                try:
                    _, identity = request('/__evaluation', {})
                    if identity.get('nonce') != 'recall-scope-local-fixture':
                        raise RuntimeError('wrong fixture server')
                    return child, stream
                except OSError:
                    if child.poll() is not None or time.monotonic()>limit:
                        raise RuntimeError(label+' not ready')
                    time.sleep(.1)
        p, log = restart_server('reprojection')
        _, projection_identity = request('/__evaluation',{})
        with sqlite3.connect(overlay) as db:
            report['native_projection'] = {'writer':projection_identity['writer'], 'actor_rows':db.execute('SELECT count(*) FROM actor_overlay WHERE did IN (?,?)',(target,display_only)).fetchone()[0], 'prefix_rows':db.execute('SELECT count(*) FROM prefix_overlay WHERE did IN (?,?)',(target,display_only)).fetchone()[0]}
        case('native-reprojection-rescues','Justin Fa',[target,display_only],group='snapshot-overlay-lifecycle')
        case('native-reprojection-rescues-scoped','Justin Fa',[target,display_only],pds='scope.test',group='snapshot-overlay-lifecycle')
        stop(p)
        log.close()
        feed.write_text(json.dumps([{'did':did,'blob_hex':blobs[did].hex(),'updated_at':update_stamp+1} for did in [target,display_only]]))
        p, log = restart_server('timestamp-only-reprojection')
        _, projection_identity = request('/__evaluation',{})
        report['timestamp_only_reprojection'] = projection_identity['writer']
        stop(p)
        log.close()
        with sqlite3.connect(source) as db:
            db.executemany('UPDATE actors SET hidden=1,updated_at=? WHERE did=?', [(update_stamp,did) for did in veil])
            db.executemany('DELETE FROM actors WHERE did=?', [(did,) for did in churn])
            db.executemany('UPDATE actors SET updated_at=? WHERE did=?',[(update_stamp+1,did) for did in [target,display_only]])
        time.sleep(max(0,update_stamp+1-time.time()+.01))
        build_env['INDEX_BUILD_ROOT'] = str(scratch/'rebuilt')
        run([str(install/'bin/typeahead-ingester')], build_env, 'fixture-rebuild.log')
        rebuilt = next((scratch/'rebuilt').glob('build-*/index.db'))
        report['rebuilt_snapshot_bytes'] = rebuilt.stat().st_size
        for key in ['TYPEAHEAD_EVAL_FEED','TYPEAHEAD_EVAL_MAX_ACTORS','TYPEAHEAD_EVAL_WRITER_MODE','TYPEAHEAD_EVAL_WRITER_START','TYPEAHEAD_EVAL_BACKGROUND_SLOTS']:
            server_env.pop(key)
        server_env |= {'TYPEAHEAD_EVAL_SNAPSHOT':str(rebuilt), 'LOCAL_DB_PATH':str(scratch/'rebuilt-overlay.db')}
        p, log = restart_server('rebuilt-server')
        case('fresh-rebuild-relapses','Justin Fa',[target,display_only],group='snapshot-overlay-lifecycle')
        case('rebuild-refills-hidden','veil unique',[visible],group='snapshot-overlay-lifecycle')
        case('rebuild-refills-deleted','churn',[churn_target],group='snapshot-overlay-lifecycle')
        report['matches_oracle'] = sum(x['matches_oracle'] for x in report['cases'])
        report['mismatches'] = sum(not x['matches_oracle'] for x in report['cases'])
        report['complete'] = True
        stop(p)
        log.close()
    except BaseException as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
        raise
    finally:
        for p in reversed(children):
            stop(p)
        report['scratch_bytes_before_cleanup'] = sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file())
        shutil.rmtree(scratch)
        report['scratch_removed'] = not scratch.exists()
        report['children_terminal'] = all(p.poll() is not None for p in children)
        report['wall_seconds'] = time.monotonic()-started
        save()
        print(json.dumps({k:report.get(k) for k in ('complete','actor_count','matches_oracle','mismatches','error','scratch_removed','children_terminal')}))


if __name__ == '__main__':
    main()

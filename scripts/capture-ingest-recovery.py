"""Read-only relay capture for current-state reconciliation, never event replay.

uv run --with websockets --with cbor2 scripts/capture-ingest-recovery.py \
    --start 33310380734 --end 33314565292 --db /tmp/typeahead-recovery.sqlite

The interval is a conservative superset of the discarded queue entries. Only
DIDs, reasons and sequence bounds are stored; historical profile values are not.
Restarting resumes the last committed frame. Non-data relay messages stop the
capture rather than silently accepting a truncated replay.
"""
import argparse
import asyncio
import io
import json
import sqlite3
import time

import cbor2
import websockets

EXACT = {'app.bsky.actor.profile', 'app.bsky.feed.post', 'app.bsky.feed.like',
         'app.bsky.graph.follow', 'sh.tangled.actor.profile'}


def wanted(collection):
    return collection in EXACT or collection.startswith(('site.standard.', 'place.birds.'))


async def capture(args):
    db = sqlite3.connect(args.db, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute('CREATE TABLE IF NOT EXISTS candidates (did TEXT PRIMARY KEY, first_seq INTEGER, last_seq INTEGER, profile INTEGER, account INTEGER, identity INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER, frames INTEGER, start_seq INTEGER, end_seq INTEGER)')
    row = db.execute('SELECT cursor,frames,start_seq,end_seq FROM progress WHERE id=1').fetchone()
    if row and row[2:] != (args.start, args.end):
        raise ValueError('Existing capture has different bounds')
    cursor, frames = row[:2] if row else (args.start, 0)
    last_report = time.monotonic()
    while cursor < args.end:
        try:
            async with websockets.connect(f'wss://bsky.network/xrpc/com.atproto.sync.subscribeRepos?cursor={cursor}', max_size=16*1024*1024, open_timeout=20, max_queue=16) as ws:
                async for raw in ws:
                    stream = io.BytesIO(raw)
                    header, event = cbor2.load(stream), cbor2.load(stream)
                    if header.get('op') != 1 or header.get('t') == '#info':
                        raise RuntimeError(f'Relay refused complete replay: {header} {event}')
                    seq = event.get('seq')
                    if seq is None:
                        raise RuntimeError(f'Missing sequence in {header}')
                    if seq < cursor:
                        raise RuntimeError('Relay sequence moved backwards')
                    if seq > args.end:
                        cursor = args.end
                        break
                    kind = header.get('t')
                    did, profile, account, identity = None, 0, 0, 0
                    if kind == '#commit':
                        if event.get('tooBig'):
                            raise RuntimeError(f'Commit {seq} requires separate repository fetch')
                        for op in event.get('ops', []):
                            col = op.get('path', '').split('/')[0]
                            if wanted(col) and op.get('action') != 'delete':
                                did = event.get('repo')
                                profile |= int(col in {'app.bsky.actor.profile', 'sh.tangled.actor.profile'})
                    elif kind in {'#identity', '#account'}:
                        did = event.get('did')
                        identity, account = int(kind == '#identity'), int(kind == '#account')
                    if did:
                        db.execute('INSERT INTO candidates VALUES (?,?,?,?,?,?) ON CONFLICT(did) DO UPDATE SET last_seq=excluded.last_seq, profile=max(profile,excluded.profile), account=max(account,excluded.account), identity=max(identity,excluded.identity)', (did,seq,seq,profile,account,identity))
                    cursor = seq
                    frames += 1
                    if time.monotonic() - last_report >= 10:
                        db.execute('INSERT OR REPLACE INTO progress VALUES (1,?,?,?,?)',(cursor,frames,args.start,args.end))
                        db.commit()
                        n = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
                        print(json.dumps({'cursor':cursor,'frames':frames,'candidates':n,'fraction':round((cursor-args.start)/(args.end-args.start),4),'source_time':event.get('time')}),flush=True)
                        last_report = time.monotonic()
                    if cursor >= args.end:
                        break
        except (websockets.ConnectionClosed, OSError, TimeoutError) as exc:
            print(json.dumps({'reconnecting':type(exc).__name__,'cursor':cursor}),flush=True)
            await asyncio.sleep(2)
    db.execute('INSERT OR REPLACE INTO progress VALUES (1,?,?,?,?)',(cursor,frames,args.start,args.end))
    db.commit()
    print(json.dumps({'complete':True,'cursor':cursor,'frames':frames,'candidates':db.execute('SELECT count(*) FROM candidates').fetchone()[0]}),flush=True)
    db.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--start',type=int,required=True)
    p.add_argument('--end',type=int,required=True)
    p.add_argument('--db',required=True)
    asyncio.run(capture(p.parse_args()))

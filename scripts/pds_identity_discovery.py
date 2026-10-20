"""Bounded source-change discovery into the cloud identity retry database.

A delayed cutoff and overlapping windows reduce commit races. Periodic missing-PDS
reconciliation is still required for backdated writes; this is not a change log.
"""
import sqlite3
from contextlib import closing
from pds_identity_queue import IdentityQueue, ENQUEUE_SQL

SELECT_SQL = '''SELECT did,pds,updated_at FROM actors INDEXED BY idx_actors_updated_at
WHERE updated_at >= :at AND (updated_at,did) > (:at,:did)
AND updated_at < :end ORDER BY updated_at,did LIMIT :cap'''


class Discovery:
    def __init__(self, path):
        IdentityQueue(path)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS identity_discovery(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                start INTEGER,end INTEGER,at INTEGER,did TEXT,complete INTEGER)''')

    def begin(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError('Explicit source window required')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT start,end,complete FROM identity_discovery').fetchone()
            if old:
                if old[:2] == (start, end):
                    return
                if not old[2] or start > old[1] or end <= old[1]:
                    raise ValueError('Incomplete, gapped or regressing discovery window')
            db.execute('INSERT OR REPLACE INTO identity_discovery VALUES(1,?,?,?,?,0)',
                       (start, end, start, ''))

    def request(self, cap=500):
        if type(cap) is not int or not 1 <= cap <= 1000:
            raise ValueError('Bounded discovery page required')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT at,did,end,complete FROM identity_discovery').fetchone()
        if row is None:
            raise ValueError('Discovery window not initialized')
        if row[3]:
            return None
        return {'at': row[0], 'did': row[1], 'end': row[2], 'cap': cap}

    def accept(self, request, rows, now):
        if not 1 <= request['cap'] <= 1000 or len(rows) > request['cap']:
            raise ValueError('Oversized discovery page')
        cursor = (request['at'], request['did'])
        missing = []
        for did, pds, at in rows:
            if (not isinstance(did, str) or not did.startswith('did:')
                    or type(at) is not int or not isinstance(pds, str)
                    or not cursor < (at, did) or at >= request['end']):
                raise ValueError('Malformed or unordered source page')
            cursor = (at, did)
            if pds == '':
                missing.append((did, at, now))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT at,did,end,complete FROM identity_discovery').fetchone()
            if old != (request['at'], request['did'], request['end'], 0):
                raise ValueError('Discovery cursor changed')
            db.executemany(ENQUEUE_SQL, missing)
            db.execute('UPDATE identity_discovery SET at=?,did=?,complete=?',
                       (*cursor, int(not rows)))
        return len(missing)

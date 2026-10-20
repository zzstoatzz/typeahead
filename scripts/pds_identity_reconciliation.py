"""Bounded cloud sweeps for missing PDS identities regardless of source timestamps."""
import sqlite3
from contextlib import closing
from pds_identity_queue import IdentityQueue, ENQUEUE_SQL

SELECT_SQL = '''SELECT rowid,did,pds,updated_at FROM actors
WHERE rowid > :after AND rowid <= :end ORDER BY rowid LIMIT :cap'''


class Reconciliation:
    def __init__(self, path):
        IdentityQueue(path)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS identity_reconciliation(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                cycle INTEGER,at INTEGER,end INTEGER,complete INTEGER)''')

    def begin(self, cycle, end):
        if type(cycle) is not int or type(end) is not int or cycle < 1 or end < 0:
            raise ValueError('Explicit cycle and source rowid boundary required')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT cycle,end,complete FROM identity_reconciliation').fetchone()
            if old:
                if old[:2] == (cycle, end):
                    return
                if not old[2] or cycle <= old[0]:
                    raise ValueError('Incomplete or regressing sweep')
            db.execute('INSERT OR REPLACE INTO identity_reconciliation VALUES(1,?,0,?,0)', (cycle,end))

    def request(self, cap=500):
        if type(cap) is not int or not 1 <= cap <= 1000:
            raise ValueError('Bounded reconciliation page required')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT cycle,at,end,complete FROM identity_reconciliation').fetchone()
        if row is None:
            raise ValueError('Sweep not initialized')
        if row[3]:
            return None
        return {'cycle':row[0], 'after':row[1], 'end':row[2], 'cap':cap}

    def accept(self, request, rows, now):
        if type(request['cap']) is not int or not 1 <= request['cap'] <= 1000 or len(rows)>request['cap']:
            raise ValueError('Oversized reconciliation page')
        cursor = request['after']
        missing = []
        for rowid,did,pds,version in rows:
            if (type(rowid) is not int or not cursor < rowid <= request['end']
                    or not isinstance(did,str) or not did.startswith('did:')
                    or not isinstance(pds,str) or type(version) is not int or version<0):
                raise ValueError('Malformed or unordered source page')
            cursor = rowid
            if pds == '':
                missing.append((did,version,now))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT cycle,at,end,complete FROM identity_reconciliation').fetchone()
            if old != (request['cycle'],request['after'],request['end'],0):
                raise ValueError('Reconciliation cursor changed')
            db.executemany(ENQUEUE_SQL,missing)
            db.execute('UPDATE identity_reconciliation SET at=?,complete=?', (cursor,int(not rows)))
        return len(missing)

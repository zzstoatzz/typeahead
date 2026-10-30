"""Durable cloud identity verification queue; independent of profile timestamps."""
import sqlite3
import time
import uuid
from contextlib import closing


class Queue:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS identities(
                    did TEXT PRIMARY KEY,
                    due_at INTEGER NOT NULL,
                    verified_at INTEGER,
                    verified_pds TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_until INTEGER,
                    lease_token TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS identities_due ON identities(state,due_at,did);
                CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
            ''')

    def discover(self, dids, cursor, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT OR IGNORE INTO identities(did,due_at) VALUES(?,?)', [(did, now) for did in dids])
            db.execute("INSERT INTO cursors VALUES('source_rowid',?) ON CONFLICT(name) DO UPDATE SET value=MAX(value,excluded.value)", (cursor,))

    def cursor(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT value FROM cursors WHERE name='source_rowid'").fetchone()
            return row[0] if row else 0

    def claim(self, limit=100, now=None, lease_seconds=600):
        if not 1 <= limit <= 100 or lease_seconds < 1:
            raise ValueError('Invalid claim bound')
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE identities SET state='pending',lease_until=NULL,lease_token=NULL WHERE state='resolving' AND lease_until<=?", (now,))
            rows = db.execute("SELECT did FROM identities WHERE state='pending' AND due_at<=? ORDER BY due_at,did LIMIT ?", (now, limit)).fetchall()
            claims = [{"did": row[0], "token": str(uuid.uuid4())} for row in rows]
            db.executemany("UPDATE identities SET state='resolving',lease_until=?,lease_token=?,attempts=attempts+1 WHERE did=?", [(now + lease_seconds, claim["token"], claim["did"]) for claim in claims])
            return claims

    def begin_write(self, did, token, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("UPDATE identities SET state='writing',lease_until=NULL WHERE did=? AND lease_token=? AND state='resolving' AND lease_until>?", (did, token, now))
            if result.rowcount != 1:
                raise ValueError('Identity is not claimed')

    def verified(self, did, token, pds, observed_at, interval=604800):
        if not pds or interval < 1:
            raise ValueError('Verified PDS and positive interval required')
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("UPDATE identities SET state='pending',verified_at=?,verified_pds=?,due_at=?,lease_until=NULL,lease_token=NULL,attempts=0,last_error=NULL WHERE did=? AND lease_token=? AND (state='writing' OR (state='resolving' AND lease_until>?))", (observed_at, pds, observed_at + interval, did, token, observed_at))
            if result.rowcount != 1:
                raise ValueError('Identity is not in a verifiable state')

    def failed(self, did, token, error, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT attempts,state FROM identities WHERE did=? AND lease_token=?', (did, token)).fetchone()
            if not row or row[1] not in ('resolving', 'writing'):
                raise ValueError('Identity is not claimed')
            state = 'uncertain' if row[1] == 'writing' else 'pending'
            delay = min(86400, 60 * 2 ** min(row[0], 10))
            db.execute('UPDATE identities SET state=?,due_at=?,lease_until=NULL,last_error=? WHERE did=?', (state, now + delay, error[:256], did))

    def conflict(self, did, token, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("UPDATE identities SET state='pending',due_at=?,lease_until=NULL,lease_token=NULL,last_error='source conflict' WHERE did=? AND lease_token=? AND state='writing'", (now + 60, did, token))
            if result.rowcount != 1:
                raise ValueError('Identity write is not owned')

    def metrics(self, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db:
            counts = dict(db.execute('SELECT state,count(*) FROM identities GROUP BY state'))
            oldest = db.execute("SELECT min(due_at) FROM identities WHERE state='pending' AND due_at<=?", (now,)).fetchone()[0]
            verified = db.execute('SELECT count(*),min(verified_at) FROM identities WHERE verified_at IS NOT NULL').fetchone()
            return {'states': counts, 'oldest_due_age_s': None if oldest is None else now-oldest, 'verified_count': verified[0], 'oldest_verification_age_s': None if verified[1] is None else now-verified[1]}

"""Cloud-local identity retries; no source writer or enabled worker.

Lease expiry permits rechecking identity, never blind replay of a source write.
The repair ledger must reconcile sent/unknown writes before another attempt.
Source updated_at is a discovery hint, not proof of unchanged identity.
"""
import sqlite3
import uuid
from contextlib import closing


ENQUEUE_SQL = """INSERT INTO identity_jobs(did,version,due) VALUES(?,?,?)
ON CONFLICT(did) DO UPDATE SET version=excluded.version,
completed_version=CASE WHEN excluded.version=identity_jobs.version
    THEN -1 ELSE identity_jobs.completed_version END,
due=min(identity_jobs.due,excluded.due)
WHERE excluded.version > identity_jobs.version
OR (excluded.version=identity_jobs.version
    AND identity_jobs.completed_version=identity_jobs.version)"""


class IdentityQueue:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS identity_jobs (
                    did TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    completed_version INTEGER NOT NULL DEFAULT -1,
                    due INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease TEXT, lease_until INTEGER, leased_version INTEGER
                );
                CREATE INDEX IF NOT EXISTS identity_due ON identity_jobs(due)
                    WHERE version > completed_version;
            ''')

    def enqueue(self, rows, now):
        if len(rows) > 1000:
            raise ValueError('Discovery page exceeds 1000 actors')
        if any(not isinstance(did, str) or not did.startswith('did:')
               or type(version) is not int or version < 0 for did, version in rows):
            raise ValueError('Invalid source identity observation')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany(ENQUEUE_SQL, [(did, version, now) for did, version in rows])

    def claim(self, now, *, limit, lease_seconds):
        if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 3600:
            raise ValueError('Bounded claim and lease required')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT did,version FROM identity_jobs
                WHERE version > completed_version AND due <= ?
                AND (lease IS NULL OR lease_until <= ?) ORDER BY due,did LIMIT ?''',
                (now, now, limit)).fetchall()
            claims = []
            for did, version in rows:
                token = str(uuid.uuid4())
                db.execute('''UPDATE identity_jobs SET lease=?,lease_until=?,
                    leased_version=?,attempts=attempts+1 WHERE did=?''',
                    (token, now + lease_seconds, version, did))
                claims.append({'did': did, 'version': version, 'lease': token})
            return claims

    def finish(self, claim, now, *, verified, retry_seconds=3600):
        if type(verified) is not bool or not 1 <= retry_seconds <= 86400:
            raise ValueError('Explicit verification and bounded retry required')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT version,leased_version,lease_until FROM identity_jobs
                WHERE did=? AND lease=?''', (claim['did'], claim['lease'])).fetchone()
            if row is None or row[1] != claim['version'] or row[2] <= now:
                raise ValueError('Expired or superseded identity lease')
            completed = claim['version'] if verified else -1
            due = now if row[0] > claim['version'] else now + retry_seconds
            db.execute('''UPDATE identity_jobs SET completed_version=max(completed_version,?),
                due=?,lease=NULL,lease_until=NULL,leased_version=NULL WHERE did=?''',
                (completed, due, claim['did']))

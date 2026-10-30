"""Durable attempt records for PDS-only repair; no transport or live CLI."""
import json
import sqlite3
import time
import uuid
from contextlib import closing

SERVER_APPLY_SQL = """UPDATE actors SET pds=:pds, updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE did=:did AND pds=:observed_pds AND updated_at=:observed_updated_at
RETURNING did,pds,updated_at"""

BATCH_APPLY_SQL = """UPDATE actors SET
pds=json_extract(j.value,'$.pds'),updated_at=CAST(strftime('%s','now') AS INTEGER)
FROM json_each(:updates) AS j
WHERE actors.did=json_extract(j.value,'$.did') AND actors.pds=json_extract(j.value,'$.observed_pds')
AND actors.updated_at=json_extract(j.value,'$.observed_updated_at')
RETURNING did,pds,updated_at"""


class Ledger:
    def __init__(self, path):
        self.path=path
        with closing(sqlite3.connect(path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS attempts(
              id TEXT PRIMARY KEY,did TEXT NOT NULL,observed_updated_at INTEGER NOT NULL,
              pds TEXT NOT NULL,prepared_at REAL NOT NULL,state TEXT NOT NULL,
              receipt TEXT, UNIQUE(did)
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS attempt_history AS
                SELECT * FROM attempts WHERE 0''')
            for table in ('attempts', 'attempt_history'):
                if 'observed_pds' not in {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}:
                    db.execute('ALTER TABLE ' + table + " ADD COLUMN observed_pds TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS attempt_history_id ON attempt_history(id)')
            db.commit()

    def prepare(self, did, observed_updated_at, pds, observed_pds=''):
        attempt=str(uuid.uuid4())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO attempts(id,did,observed_updated_at,pds,prepared_at,state,receipt,observed_pds) VALUES(?,?,?,?,?,?,NULL,?)',
                       (attempt,did,observed_updated_at,pds,time.time(),'prepared',observed_pds))
        return attempt

    def get(self, attempt):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM attempts WHERE id=? UNION ALL SELECT * FROM attempt_history WHERE id=?',(attempt,attempt)).fetchone()
            if row is None:raise ValueError('Unknown repair attempt')
            return dict(row)

    def current(self, did):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM attempts WHERE did=?',(did,)).fetchone()
            return dict(row) if row else None

    def retry(self, previous, observed_updated_at, pds, observed_pds=''):
        if type(observed_updated_at) is not int or observed_updated_at < 0 or not isinstance(pds,str) or not pds:
            raise ValueError('Fresh source timestamp and resolved PDS required')
        attempt=str(uuid.uuid4())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            item=db.execute('SELECT did,state FROM attempts WHERE id=?',(previous,)).fetchone()
            if item is None or item[1] not in ('confirmed','conflict'):
                raise ValueError('Retry requires a definite completed current attempt')
            db.execute('INSERT INTO attempt_history SELECT * FROM attempts WHERE id=?',(previous,))
            db.execute('DELETE FROM attempts WHERE id=?',(previous,))
            db.execute('INSERT INTO attempts(id,did,observed_updated_at,pds,prepared_at,state,receipt,observed_pds) VALUES(?,?,?,?,?,?,NULL,?)',
                       (attempt,item[0],observed_updated_at,pds,time.time(),'prepared',observed_pds))
        return attempt

    def transition(self, attempt, before, after, receipt=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            result=db.execute('UPDATE attempts SET state=?,receipt=? WHERE id=? AND state=?',
                (after,json.dumps(receipt) if receipt is not None else None,attempt,before))
            if result.rowcount != 1:raise ValueError('Repair state changed; refusing operation')

    def send(self, attempt, execute):
        item=self.get(attempt)
        self.transition(attempt,'prepared','sent')
        try:
            rows=execute(SERVER_APPLY_SQL,{k:item[k] for k in ('did','pds','observed_updated_at','observed_pds')})
            if rows == []:
                self.transition(attempt,'sent','conflict',[])
                return 'conflict'
            if (len(rows)!=1 or len(rows[0])!=3 or rows[0][0]!=item['did']
                    or rows[0][1]!=item['pds'] or not isinstance(rows[0][2],int)):
                raise ValueError('Unrecognized mutation receipt')
            self.transition(attempt,'sent','confirmed',rows[0])
            return 'confirmed'
        except Exception:
            # Even a transport error can follow a committed write. Never replay it blindly.
            if self.get(attempt)['state']=='sent':self.transition(attempt,'sent','unknown')
            raise

    def send_batch(self, attempts, execute):
        if not 1<=len(attempts)<=100 or len(set(attempts))!=len(attempts):
            raise ValueError('Batch requires one to 100 distinct attempts')
        items=[self.get(a) for a in attempts]
        with closing(sqlite3.connect(self.path)) as db,db:
            for item in items:
                result=db.execute("UPDATE attempts SET state='sent' WHERE id=? AND state='prepared'",(item['id'],))
                if result.rowcount!=1:raise ValueError('Batch has a non-prepared attempt')
        by_did={r['did']:r for r in items}
        try:
            payload=[{k:item[k] for k in ('did','pds','observed_updated_at','observed_pds')} for item in items]
            rows=execute(BATCH_APPLY_SQL,{'updates':json.dumps(payload)})
            receipts={}
            for row in rows:
                if (len(row)!=3 or row[0] not in by_did or row[0] in receipts
                        or row[1]!=by_did[row[0]]['pds'] or not isinstance(row[2],int)):
                    raise ValueError('Unrecognized batch receipt')
                receipts[row[0]]=row
            with closing(sqlite3.connect(self.path)) as db,db:
                for item in items:
                    row=receipts.get(item['did'])
                    result=db.execute("UPDATE attempts SET state=?,receipt=? WHERE id=? AND state='sent'",
                        ('confirmed' if row else 'conflict',json.dumps(row),item['id']))
                    if result.rowcount!=1:raise ValueError('Batch receipt state changed')
            return {'confirmed':len(receipts),'conflict':len(items)-len(receipts)}
        except Exception:
            with closing(sqlite3.connect(self.path)) as db,db:
                db.executemany("UPDATE attempts SET state='unknown' WHERE id=? AND state='sent'",[(a,) for a in attempts])
            raise

    def reconcile(self, attempt, current):
        item=self.get(attempt)
        if item['state'] not in ('sent','unknown'):
            raise ValueError('Only unresolved attempts may be reconciled')
        if current is None:
            outcome='deleted'
        elif current['pds']==item['pds']:
            outcome='present_unattributed'
        elif current['pds']==item['observed_pds'] and current['updated_at']==item['observed_updated_at']:
            outcome='unchanged'
        else:
            outcome='changed'
        self.transition(attempt,item['state'],outcome,current)
        return outcome

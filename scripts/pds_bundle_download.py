"""Durable conservative transfer budget used by PLC tail capture."""
import sqlite3
from contextlib import closing


class Budget:
    def __init__(self,path,limit):
        if limit<1:raise ValueError('Positive byte budget required')
        self.path=path
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK(id=1),limit_bytes INTEGER, reserved INTEGER)')
            db.execute('INSERT OR IGNORE INTO budget VALUES(1,?,0)',(limit,))
            if db.execute('SELECT limit_bytes FROM budget').fetchone()[0]!=limit:
                raise ValueError('Refusing to change an existing transfer budget')

    def reserve(self,count):
        if count<1:raise ValueError('Positive reservation required')
        with closing(sqlite3.connect(self.path)) as db,db:
            row=db.execute('UPDATE budget SET reserved=reserved+? WHERE id=1 AND reserved+?<=limit_bytes RETURNING reserved',(count,count)).fetchone()
            if row is None:raise RuntimeError('Transfer byte budget exhausted')

    def used(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT reserved FROM budget').fetchone()[0]

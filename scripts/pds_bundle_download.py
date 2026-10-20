"""Bounded, checksum-verified downloads with a durable conservative byte budget."""
import hashlib
import os
import sqlite3
import time
import urllib.request
import urllib.error
from contextlib import closing
from pathlib import Path


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


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def download(url,target,size,sha256,budget,bytes_per_second=5*1024*1024):
    target=Path(target)
    if size<1 or bytes_per_second<1:raise ValueError('Positive size and rate required')
    if target.exists():
        if target.stat().st_size!=size or digest(target)!=sha256:raise ValueError('Existing bundle differs from expected artifact')
        return {'cached':True,'bytes':0}
    partial=target.with_name(target.name+'.part')
    if partial.exists():raise ValueError('Existing partial requires explicit reconciliation')
    read=0;started=time.monotonic();h=hashlib.sha256()
    # Reserve the complete object plus one overrun-detection byte before opening
    # the connection. Failed attempts remain charged; crash/retry cannot hide use.
    budget.reserve(size+1)
    with partial.open('xb') as output:
        request=urllib.request.Request(url,headers={'Accept-Encoding':'identity','User-Agent':'typeahead-pds-repair'})
        try:
            response=urllib.request.urlopen(request,timeout=60)
        except urllib.error.HTTPError as error:
            error.close()
            raise
        with response:
            if response.status!=200:raise ValueError('Unexpected download status')
            length=response.headers.get('Content-Length')
            if length is not None and int(length)!=size:raise ValueError('Unexpected content length')
            while True:
                block=response.read(min(1024*1024,size+1-read))
                if not block:break
                read+=len(block)
                if read>size:raise ValueError('Bundle exceeds expected size')
                output.write(block);h.update(block)
                delay=read/bytes_per_second-(time.monotonic()-started)
                if delay>0:time.sleep(delay)
        output.flush();os.fsync(output.fileno())
    if read!=size or h.hexdigest()!=sha256:raise ValueError('Bundle checksum/size mismatch')
    partial.rename(target)
    return {'cached':False,'bytes':read,'reserved_total':budget.used()}

from pathlib import Path
import json
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent


def blob(handle, hidden=0, score=1):
    fields=[handle,'Name','','[]','https://small.test/','','{}']
    return bytes([1,hidden])+struct.pack('<f',score)+b''.join(struct.pack('<H',len(f.encode()))+f.encode() for f in fields)


def varint(n):
    out=bytearray()
    while n>=128:out.append((n&127)|128);n>>=7
    out.append(n)
    return out


def flat(path,rows):
    rows=sorted(rows.items());keys=[]
    for did,b in rows:
        key=b'\x01'+did.encode()
        keys.append(bytes(varint(0)+varint(len(key)))+b'\x00\x00'+key)
    end=18+sum(map(len,keys));position=end;encoded=bytearray();postings=bytearray()
    for key,(_,value) in zip(keys,rows):
        key=bytearray(key);p=2;struct.pack_into('<H',key,p,position)
        encoded.extend(key);postings.extend(struct.pack('<I',len(value))+value);position+=4+len(value)
    page=struct.pack('<IIIIH',len(rows),end,position,0,18)+encoded+postings
    page+=bytes(8192-len(page))
    first=b'\x01'+rows[0][0].encode();record=struct.pack('<H',len(first))+first+struct.pack('<Q',0)
    header=struct.pack('<8sIIQQQQQQQQ',b'FLATPX05',5,1,0,88,4096,12288,0,0,0,0)
    root=header+struct.pack('<II',0,len(record))+record
    path.write_bytes(root+bytes(4096-len(root))+page)


class Export(unittest.TestCase):
    def test_overlay_rename_tombstone_hidden_invalid_and_locations(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            flat(p/'snapshot',{'did:web:a':blob('old.test'),'did:web:b':blob('delete.test'),'did:web:c':blob('stay.test'),'did:web:d':blob('hidden.test',1)})
            with sqlite3.connect(p/'overlay') as db:
                db.execute('CREATE TABLE actor_overlay(did TEXT PRIMARY KEY,blob BLOB)')
                db.execute('CREATE TABLE prefix_overlay(k TEXT)')
                db.executemany('INSERT INTO actor_overlay VALUES(?,?)',[('did:web:a',blob('new.test',score=7)),('did:web:b',None),('did:web:e',blob('handle.invalid')),('did:web:f',blob('new-hidden.test',1))])
            result=subprocess.run([sys.executable,ROOT/'export.py',p/'snapshot',p/'overlay',p/'out'],check=True,capture_output=True,text=True)
            report=json.loads(result.stdout)
            self.assertEqual(report['actors'],2)
            lines=[r.split('\t') for r in (p/'out/unsorted.tsv').read_text().splitlines()]
            self.assertEqual({r[0] for r in lines},{'new.test','stay.test'})
            for handle,did,score,pds,offset,length,source in lines:
                self.assertEqual(pds,'small.test')
                content=(p/'snapshot' if source=='0' else p/'out/overlay-profiles.bin').read_bytes()
                b=content[int(offset):int(offset)+int(length)]
                self.assertEqual(b,blob(handle,score=float(score)))


if __name__=='__main__':unittest.main()

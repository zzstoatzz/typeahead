import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('fallback_sync',ROOT/'sync.py')
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'pds.sqlite'
        with sqlite3.connect(self.path) as db:
            db.executescript('PRAGMA user_version=1; CREATE TABLE pds_fallback(did TEXT PRIMARY KEY,pds TEXT NOT NULL) WITHOUT ROWID;')
            db.execute('INSERT INTO pds_fallback VALUES (?,?)',('did:plc:known','https://archive.example'))
        self.sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.row={'did':'did:plc:known','handle':'account.example','updated_at':12,'pds':''}

    def test_repeat_observation_migration_deletion_and_disabled_mode(self):
        self.assertEqual(sync.actor_record(self.row)['pds'],'')
        for _ in range(2):
            with sync.pds_fallback(self.path,self.sha):
                first=sync.actor_record(self.row)
                self.assertEqual(first['pds'],'https://archive.example')
                self.assertEqual(first['version'],12)
                changed=sync.actor_record(dict(self.row,display_name='changed'))
                self.assertEqual(changed['pds'],first['pds'])
                self.assertEqual(sync.actor_record(dict(self.row,pds='https://current.example'))['pds'],'https://current.example')
                self.assertTrue(sync.actor_record(dict(self.row,handle=''))['deleted'])
                self.assertEqual(sync.actor_record(dict(self.row,did='did:plc:absent'))['pds'],'')
        self.assertEqual(sync.actor_record(self.row)['pds'],'')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.sha)

    def test_checksum_and_schema_fail_closed(self):
        with self.assertRaisesRegex(sync.SafeError,'checksum'):
            sync.PdsFallback(self.path,'0'*64)
        with sqlite3.connect(self.path) as db:db.execute('PRAGMA user_version=2')
        sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(sync.SafeError,'schema'):sync.PdsFallback(self.path,sha)

    def test_invalid_or_intermediary_hosts_are_not_fallbacks(self):
        for value in ['https://unknown','https://bsky.network','https://relay1.us-east.bsky.network','https://bridgy-hubble.microcosm.blue','https://x.example:443','https://u@x.example','https://x.example/path','https://[bad','https://x.example.','http://x.example','https://-bad.example']:
            self.assertIsNone(sync.canonical_archive_pds(value),value)
        self.assertEqual(sync.canonical_archive_pds('https://morel.us-east.host.bsky.network'),'https://morel.us-east.host.bsky.network')
        with sqlite3.connect(self.path) as db:db.execute("UPDATE pds_fallback SET pds='https://bsky.network'")
        sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with sync.pds_fallback(self.path,sha):
            with self.assertRaisesRegex(sync.SafeError,'invalid archive PDS'):sync.actor_record(self.row)

if __name__=='__main__':unittest.main()

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
sys.path.insert(0,str(REPO/'scripts/snapshot_compaction'))
from reference_build import build
spec=importlib.util.spec_from_file_location('compact_export',ROOT/'export.py')
export=importlib.util.module_from_spec(spec);spec.loader.exec_module(export)

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def packed(handle='alice.fixture.test',display='Alice 東京',avatar='https://cdn.test/a.png',labels='[{"val":"test-label"}]',pds='https://local.test',created='2026-10-28T00:00:00Z',associated='{"chat":{"allowIncoming":"all"}}',score=0.1,hidden=False):
    out=bytearray(struct.pack('<BBf',1,hidden,score))
    for value in [handle,display,avatar,labels,pds,created,associated]:
        value=value.encode();out.extend(struct.pack('<H',len(value)));out.extend(value)
    return bytes(out)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT,prefix='.test-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        source=self.root/'legacy.db'
        with sqlite3.connect(source) as db:
            db.executescript('CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY,blob BLOB NOT NULL) WITHOUT ROWID;CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY,dids BLOB NOT NULL) WITHOUT ROWID;CREATE TABLE index_meta(key TEXT PRIMARY KEY,value TEXT);')
            actors=[(f'did:example:{i:04d}',packed(handle=f'actor{i:04d}.fixture.test',hidden=i==16,avatar='https://cdn.test/'+('x'*9000 if i==18 else str(i)))) for i in range(19)]
            db.executemany('INSERT INTO actor_compact_base VALUES(?,?)',actors)
            db.execute('INSERT INTO prefix_top_base VALUES(?,?)',('n:alice',b'not consumed for actor export'))
            meta={'build_id':'fixture-px05','normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_watermark':'1793230000','actor_count':'19','prefix_count':'1'}
            db.executemany('INSERT INTO index_meta VALUES(?,?)',meta.items())
        self.snapshot=self.root/'snapshot';build(source,self.snapshot)
        self.manifest={'manifest_version':2,'format_version':5,'active_build':'fixture-px05','source_watermark':1793230000,'actor_count':19,'prefix_count':1,'normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','byte_size':(self.snapshot/'index.db').stat().st_size,'sha256':digest(self.snapshot/'index.db'),'prefix_map':{'object_key':'releases/search/fixture-px05/index.db.prefix','byte_size':(self.snapshot/'index.db.prefix').stat().st_size,'sha256':digest(self.snapshot/'index.db.prefix')}}
        self.manifest_path=self.root/'release.json';self.write_manifest()
    def write_manifest(self):self.manifest_path.write_text(json.dumps(self.manifest))
    def args(self,*extra):
        return export.parser().parse_args(['--manifest',str(self.manifest_path),'--database',str(self.snapshot/'index.db'),'--prefix',str(self.snapshot/'index.db.prefix'),'--build-id','fixture-px05','--scratch-root',str(self.root),'--output','-','--receipt',str(self.root/'receipt.json'),*extra])
    def resign(self):
        sha=digest(self.snapshot/'index.db.prefix');self.manifest['prefix_map']['sha256']=sha;self.manifest['prefix_map']['byte_size']=(self.snapshot/'index.db.prefix').stat().st_size
        with sqlite3.connect(self.snapshot/'index.db') as db:db.execute("UPDATE index_meta SET value=? WHERE key='flat_prefix_sha256'",(sha,))
        self.manifest['sha256']=digest(self.snapshot/'index.db');self.manifest['byte_size']=(self.snapshot/'index.db').stat().st_size;self.write_manifest()
    def test_actual_reference_writer_roundtrip_hydration_and_footer(self):
        output=io.BytesIO();receipt=export.run_export(self.args(),output);lines=output.getvalue().splitlines(keepends=True);rows=[json.loads(x) for x in lines]
        self.assertEqual(rows[0]['actor_count'],19);self.assertFalse(rows[0]['partial']);self.assertEqual(rows[-1]['type'],'end')
        self.assertEqual([x['did'] for x in rows[1:-1]],[f'did:example:{i:04d}' for i in range(19)])
        actor=rows[1];self.assertEqual(actor['display_name'],'Alice 東京');self.assertEqual(actor['avatar_url'],'https://cdn.test/0');self.assertEqual(actor['labels'],[{'val':'test-label'}]);self.assertEqual(actor['associated'],{'chat':{'allowIncoming':'all'}});self.assertEqual(actor['pds'],'https://local.test');self.assertEqual(actor['created_at'],'2026-10-28T00:00:00Z');self.assertEqual(actor['quality_score'],struct.unpack('<f',struct.pack('<f',0.1))[0]);self.assertEqual(actor['quality_score_f32_bits'],'3dcccccd');self.assertEqual(actor['version'],1793230000);self.assertIsNone(actor['source_updated_at']);self.assertEqual(actor['followers_count'],0);self.assertTrue(rows[17]['hidden']);self.assertGreater(len(rows[-2]['avatar_url']),8192)
        self.assertEqual(rows[-1]['actors_sha256'],hashlib.sha256(b''.join(lines[1:-1])).hexdigest());self.assertEqual(receipt['actors_sha256'],rows[-1]['actors_sha256']);self.assertTrue(receipt['source_unchanged'])
    def test_partial_is_explicit_and_output_is_atomic(self):
        path=self.root/'actors.jsonl';receipt=export.run_export(self.args('--output',str(path),'--limit-actors','3'))
        rows=[json.loads(x) for x in path.read_bytes().splitlines()];self.assertTrue(rows[0]['partial']);self.assertEqual(rows[0]['source_actor_count'],19);self.assertEqual(rows[0]['actor_count'],3);self.assertEqual(receipt['emitted_actor_count'],3);self.assertEqual(len(rows),5);self.assertFalse(path.with_name(path.name+'.partial').exists())
    def test_wrong_release_checksum_and_truncation_fail_before_manifest(self):
        for mutate in ['build','checksum','truncate']:
            with self.subTest(mutate=mutate):
                original=copy.deepcopy(self.manifest);prefix=self.snapshot/'index.db.prefix';data=prefix.read_bytes()
                if mutate=='build':self.manifest['active_build']='other'
                elif mutate=='checksum':self.manifest['prefix_map']['sha256']='0'*64
                else:prefix.write_bytes(data[:-1])
                self.write_manifest();out=io.BytesIO()
                with self.assertRaises(ValueError):export.run_export(self.args(),out)
                self.assertEqual(out.getvalue(),b'');self.assertFalse((self.root/'receipt.json').exists());self.manifest=original;self.write_manifest();prefix.write_bytes(data)
    def test_forged_valid_digest_cannot_hide_bad_restart(self):
        prefix=self.snapshot/'index.db.prefix';data=bytearray(prefix.read_bytes());pagedata=struct.unpack_from('<Q',data,32)[0];struct.pack_into('<H',data,pagedata+16,9999);prefix.write_bytes(data);self.resign()
        path=self.root/'actors.jsonl'
        with self.assertRaisesRegex(ValueError,'restart'):export.run_export(self.args('--output',str(path)))
        self.assertFalse(path.exists());self.assertFalse(path.with_name(path.name+'.partial').exists());self.assertFalse((self.root/'receipt.json').exists())
    def test_forged_actor_count_cannot_silently_truncate(self):
        self.manifest['actor_count']=18
        with sqlite3.connect(self.snapshot/'index.db') as db:db.execute("UPDATE index_meta SET value='18' WHERE key='actor_count'")
        self.manifest['sha256']=digest(self.snapshot/'index.db');self.write_manifest();out=io.BytesIO()
        with self.assertRaisesRegex(ValueError,'more actors'):export.run_export(self.args(),out)
        self.assertNotEqual(json.loads(out.getvalue().splitlines()[-1])['type'],'end');self.assertFalse((self.root/'receipt.json').exists())
    def test_budget_failure_has_no_completion_or_output(self):
        path=self.root/'actors.jsonl'
        with self.assertRaisesRegex(ValueError,'output budget'):export.run_export(self.args('--output',str(path),'--max-output-bytes','100'))
        self.assertFalse(path.exists());self.assertFalse(path.with_name(path.name+'.partial').exists())
    def test_packed_corruption_and_nonfinite_payload_are_rejected(self):
        valid=packed()
        for blob in [valid[:-1],valid+b'x',b'\x02'+valid[1:],packed(labels='{}'),packed(associated='[]'),packed(score=float('nan')),packed(score=float('inf'))]:
            with self.subTest(blob=len(blob)):
                with self.assertRaises(ValueError):export.unpack_actor('did:example:test',blob,1)
    def test_receipt_publication_failure_retires_staged_and_final_output(self):
        output=self.root/'actors.jsonl';receipt=self.root/'missing'/'receipt.json'
        with self.assertRaises(FileNotFoundError):export.run_export(self.args('--output',str(output),'--receipt',str(receipt)))
        self.assertFalse(output.exists());self.assertFalse(output.with_name(output.name+'.partial').exists());self.assertFalse(receipt.exists())
    def test_does_not_overwrite_destination_or_accept_output_outside_scratch(self):
        out=self.root/'actors.jsonl';out.write_bytes(b'keep')
        with self.assertRaises(ValueError):export.run_export(self.args('--output',str(out)))
        self.assertEqual(out.read_bytes(),b'keep')
        with self.assertRaises(ValueError):export.run_export(self.args('--output',str(self.root.parent/'outside.jsonl')))

if __name__=='__main__':unittest.main()

import ctypes as C
import json
import math
from pathlib import Path
import struct
import unittest

ROOT=Path(__file__).resolve().parent
class Result(C.Structure):
    _fields_=[('data',C.POINTER(C.c_uint8)),('len',C.c_size_t),('owner',C.c_void_p)]
lib=C.CDLL(str(ROOT/'libtypeahead_normalizer.dylib'))
lib.ta_plan.argtypes=[C.c_void_p,C.c_size_t,C.POINTER(Result)];lib.ta_plan.restype=C.c_int32
lib.ta_result_free.argtypes=[C.POINTER(Result)]
lib.ta_normalizer_version.restype=C.c_char_p
lib.ta_plan_version.restype=C.c_char_p
lib.ta_score_f32.argtypes=[C.c_double,C.POINTER(C.c_float),C.POINTER(C.c_uint32)];lib.ta_score_f32.restype=C.c_int32

def plan(op,**values):
    raw=json.dumps({'op':op,**values},ensure_ascii=False,separators=(',',':')).encode()
    result=Result()
    code=lib.ta_plan(raw,len(raw),C.byref(result))
    if code!=0:raise ValueError('ABI failed '+str(code))
    try:return json.loads(C.string_at(result.data,result.len))
    finally:lib.ta_result_free(C.byref(result))

def candidate(actor,query):
    keys=set(actor['keys'])
    display=all(all(k in keys for k in group['keys']) for group in query['groups'])
    return display or (query['mode']=='or' and (query['handle_key'] in keys or query['domain_key'] in keys))

class AdapterTests(unittest.TestCase):
    def test_canonical_corpus(self):
        cases={'ÉRIC':'eric','François':'francois','Æneid Œuvre Straße':'aeneid oeuvre strasse',
               'Fig (aka:[phil])':'fig aka phil','e\u0301ric':'eric','Ｎａｔｅ１２３':'nate123',
               'ジョン・スミス':'じょん すみす','Tokyo東京':'tokyo東京',
               'ｶﾞ':'ｶ','nate 👋':'nate','مرحبا':''}
        for raw,expected in cases.items():
            self.assertEqual(plan('actor',display=raw)['normalized_display'],expected,raw)
        self.assertEqual(lib.ta_normalizer_version(),b'v2-cjk-bigram')
        self.assertEqual(lib.ta_plan_version(),b'complete-prefix-groups-v1')

    def test_sanitize_is_native_and_unsupported_is_explicit(self):
        self.assertEqual(plan('query',q='FOO_\\%"BAR')['normalized'],'foobar')
        self.assertEqual(plan('query',q='nate\ncode')['normalized'],'natecode')
        for raw in ['مرحبا','hello 한국','привет','שלום']:
            self.assertEqual(plan('query',q=raw)['status'],'unsupported_query')
        self.assertEqual(plan('query',q='() 👋')['status'],'empty')

    def test_complete_prefixes_all_words_and_domain(self):
        a=plan('actor',handle='abcdefghijklmnopneedle.longdomain.example.test',display='one two three four five six seven eight nine longdisplaytokenneedle')
        for key in ['h:abcdefghijklmnopneed','n:longdisplaytokenneed','d:longdomain.example.test','e:abcdefghijklmnopneedle.longdomain.example.test']:
            self.assertIn(key,a['keys'])
        self.assertEqual(len(a['keys']),len(set(a['keys'])))
        self.assertTrue(plan('matches',handle='',display='one two three four five six seven eight nine',q='one two three four five six seven eight nine')['matches'])
        self.assertFalse(plan('matches',handle='',display='one two three four five six seven eight nine',q='one two three four five six seven eight nope')['matches'])
        self.assertTrue(plan('matches',handle='foo.germ.network',q='germ.net')['matches'])

    def test_cjk_grouped_boolean_and_contiguous_verification(self):
        query=plan('query',q='日本太')
        self.assertEqual(query['mode'],'or')
        self.assertTrue(query['needs_verification'])
        self.assertEqual(query['groups'][0]['keys'],['n:日本','n:本太'])
        valid=plan('actor',display='名前日本太郎')
        wrong=plan('actor',display='日本 東京 本太')
        self.assertTrue(candidate(valid,query))
        self.assertTrue(candidate(wrong,query))
        self.assertTrue(plan('matches',display='名前日本太郎',q='日本太')['matches'])
        self.assertFalse(plan('matches',display='日本 東京 本太',q='日本太')['matches'])
        self.assertFalse(plan('matches',display='日本 本日',q='日本日本')['matches'])
        self.assertTrue(plan('matches',display='名前日本太郎',q='日本')['matches'])
        self.assertFalse(plan('matches',display='名前日本太郎',q='本')['matches'])
        mixed=plan('query',q='Alice日本')
        self.assertEqual(len(mixed['groups']),2)
        self.assertFalse(candidate(plan('actor',display='Alice Other'),mixed))
        self.assertTrue(candidate(plan('actor',display='Alice 日本'),mixed))
        self.assertTrue(plan('matches',display='Alice 日本',q='Alice日本')['matches'])

    def test_linear_cjk_membership_and_no_24_codepoint_cap(self):
        name=''.join(chr(0x4e00+i) for i in range(64))
        actor=plan('actor',display=name)
        self.assertEqual(actor['status'],'ok')
        self.assertEqual(len(actor['keys']),64)
        self.assertIn('n:'+name[-2:],actor['keys'])
        self.assertTrue(plan('matches',display=name,q=name[-3:])['matches'])

    def test_normalization_bounds_are_not_silent_truncation(self):
        self.assertEqual(plan('query',q='a'*256)['status'],'ok')
        self.assertEqual(plan('query',q='a'*257)['status'],'budget_exceeded')
        self.assertEqual(plan('query',q='é'*160)['normalized'],'e'*160)
        self.assertEqual(plan('query',q='a'*256+'!!!')['status'],'ok')
        self.assertEqual(plan('query',q='a'*256+'!b')['status'],'budget_exceeded')
        self.assertEqual(plan('query',q='a'*513)['status'],'budget_exceeded')
        self.assertEqual(plan('actor',display='a'*257)['status'],'budget_exceeded')

    def test_pds_normalization(self):
        self.assertEqual(plan('pds',pds='HTTPS://ACME.com./')['pds'],'acme.com')
        for value in ['acme','acme.com:443','evil@acme.com','*.acme.com','a..com','acme.com/path']:
            self.assertEqual(plan('pds',pds=value)['status'],'invalid_input')

    def test_f32_semantics_and_invalid_scores(self):
        first=plan('actor',score=16777216,display='x')
        second=plan('actor',score=16777217,display='x')
        self.assertEqual(first['score_bits'],second['score_bits'])
        self.assertEqual(plan('actor',score=0.1)['score_bits'],0x3dcccccd)
        for value in [math.inf,-math.inf,math.nan,1e300]:
            out=C.c_float();bits=C.c_uint32()
            self.assertNotEqual(lib.ta_score_f32(value,C.byref(out),C.byref(bits)),0)

    def test_result_lifetime(self):
        for i in range(200):
            result=plan('query',q='Justin Fa')
            self.assertEqual(result['normalized'],'justin fa')

if __name__=='__main__':unittest.main()

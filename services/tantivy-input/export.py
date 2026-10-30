#!/usr/bin/env python3
"""Cloud-local, bounded-memory FLATPX05 actor export; no network or credential access."""
import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import struct
import sys
import time

HEADER=struct.Struct('<8sIIIIQQQQQQQ')
MAX_KEY=8193
MAX_EXTENT=16*1024*1024
FIELDS=('handle','display_name','avatar_url','labels','pds','created_at','associated')

class Budget:
    def __init__(self,args):
        if not math.isfinite(args.max_seconds) or args.max_seconds<=0:raise ValueError('invalid export wall deadline')
        self.args=args;self.started=time.monotonic();self.read_bytes=0;self.output_bytes=0
    def guard(self):
        if time.monotonic()-self.started>self.args.max_seconds:raise TimeoutError('export wall deadline exceeded')
    def read(self,f,offset,size):
        self.guard()
        if offset<0 or size<0:raise ValueError('negative input range')
        f.seek(offset);data=f.read(size);self.read_bytes+=len(data)
        if len(data)!=size:raise ValueError('truncated input range')
        return data
    def digest(self,path):
        h=hashlib.sha256()
        with path.open('rb') as f:
            while chunk:=f.read(1024*1024):self.guard();self.read_bytes+=len(chunk);h.update(chunk)
        return h.hexdigest()
    def write(self,stream,data):
        self.guard()
        if self.output_bytes+len(data)>self.args.max_output_bytes:raise ValueError('export output budget exceeded')
        stream.write(data);self.output_bytes+=len(data)

def jsonline(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf8')

def integer(value,name):
    if type(value) is not int or value<0:raise ValueError('invalid '+name)
    return value

def checksum(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('invalid SHA256')
    return value

def identity(path):
    s=path.stat()
    if not stat.S_ISREG(s.st_mode):raise ValueError('input must be a regular cloud-local file')
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def validate_release(args,budget):
    manifest_path=Path(args.manifest).resolve();database=Path(args.database).resolve();prefix=Path(args.prefix).resolve()
    if manifest_path.stat().st_size>128*1024:raise ValueError('manifest exceeds128KiB')
    originals={p:identity(p) for p in [manifest_path,database,prefix]}
    manifest=json.loads(manifest_path.read_bytes())
    if manifest.get('manifest_version')!=2 or manifest.get('format_version')!=5:raise ValueError('requires compact manifest version2/format5')
    build=manifest.get('active_build')
    if not isinstance(build,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',build) or build!=args.build_id:raise ValueError('release build identity differs')
    wm=integer(manifest.get('source_watermark'),'source_watermark');count=integer(manifest.get('actor_count'),'actor_count')
    if wm==0 or count>args.max_actors:raise ValueError('watermark or actor-count budget invalid')
    side=manifest.get('prefix_map',{})
    expected=[(database,integer(manifest.get('byte_size'),'database size'),checksum(manifest.get('sha256'))),(prefix,integer(side.get('byte_size'),'prefix size'),checksum(side.get('sha256')))]
    if sum(n for _,n,_ in expected)>args.max_input_bytes:raise ValueError('source size budget exceeded')
    verification=getattr(args,'verified_inputs',None)
    verification_sha=getattr(args,'verified_inputs_sha256',None)
    if bool(verification)!=bool(verification_sha):raise ValueError('verified inputs require path and checksum together')
    if verification:
        witness=Path(verification).absolute()
        if witness.is_symlink() or witness.resolve()!=witness or witness.parent!=manifest_path.parent or witness.stat().st_size>16384:
            raise ValueError('invalid verified-input receipt path or budget')
        if witness.stat().st_mode&0o222:raise ValueError('verified-input receipt must be read-only')
        raw=witness.read_bytes();budget.read_bytes+=len(raw)
        if hashlib.sha256(raw).hexdigest()!=checksum(verification_sha):raise ValueError('verified-input receipt checksum mismatch')
        proof=json.loads(raw)
        if proof.get('schema_version')!=1 or proof.get('build_id')!=build or proof.get('manifest_sha256')!=budget.digest(manifest_path):
            raise ValueError('verified-input release identity mismatch')
        if not isinstance(proof.get('files'),dict) or set(proof['files'])!={'database','prefix'}:raise ValueError('verified-input files mismatch')
        for kind,(path,size,sha) in zip(['database','prefix'],expected):
            record=proof['files'][kind]
            if not isinstance(record,dict) or record.get('name')!=path.name or record.get('sha256')!=sha or record.get('identity')!=list(originals[path]) or originals[path][2]!=size or path.stat().st_mode&0o222:
                raise ValueError('verified input changed or differs from release')
    else:
        for path,size,sha in expected:
            if path.stat().st_size!=size or budget.digest(path)!=sha:raise ValueError('source size/checksum mismatch: '+path.name)
    for suffix in ['-wal','-journal']:
        if Path(str(database)+suffix).exists():raise ValueError('metadata database is not a closed immutable checkpoint')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise ValueError('metadata SQLite integrity failure')
        metadata=dict(db.execute('SELECT key,value FROM index_meta'))
    for key in ['build_id','normalize_version','scoring_version','source_watermark','actor_count','prefix_count']:
        expected_value=build if key=='build_id' else manifest[key]
        if metadata.get(key)!=str(expected_value):raise ValueError('manifest/database metadata differs: '+key)
    if metadata.get('flat_prefix_version')!='5' or metadata.get('flat_prefix_sha256')!=side['sha256']:raise ValueError('flat artifact identity differs from metadata database')
    for path,before in originals.items():
        if identity(path)!=before:raise ValueError('input changed during verification')
    return manifest,prefix,originals

def varint(data,pos,end):
    value=0
    for shift in [0,7,14]:
        if pos>=end:raise ValueError('truncated flat varint')
        byte=data[pos];pos+=1;value|=(byte&127)<<shift
        if byte<128:
            if value>MAX_KEY:raise ValueError('flat key length overflow')
            return value,pos
    raise ValueError('flat varint overflow')

class FlatActors:
    def __init__(self,file,budget,max_root_bytes):
        self.file=file;self.budget=budget;self.size=os.fstat(file.fileno()).st_size
        header=budget.read(file,0,80)
        magic,version,pages,r1,r2,records,pagedata,size,*reserved=HEADER.unpack(header)
        if magic!=b'FLATPX05' or version!=5 or r1 or r2 or any(reserved):raise ValueError('invalid flat header')
        if size!=self.size or size%4096 or records!=80+4*(pages+1) or records>pagedata or pagedata>size or pagedata%4096:raise ValueError('invalid flat section bounds')
        if pagedata>max_root_bytes:raise ValueError('root-directory memory budget exceeded')
        self.pages=pages;self.records=records;self.pagedata=pagedata
        self.rootdata=budget.read(file,0,pagedata)
        if struct.unpack_from('<I',self.rootdata,80)[0]!=0:raise ValueError('first root record offset is not zero')
        sentinel=struct.unpack_from('<I',self.rootdata,80+4*pages)[0]
        if sentinel>pagedata-records:raise ValueError('root record sentinel out of bounds')
        previous=None;offset=None;self.actor_pages=0
        for i in range(pages):
            if i%1024==0:budget.guard()
            key,start=self.root(i)
            if previous is not None and key<=previous:raise ValueError('root keys not strictly ordered')
            if offset is None and start!=pagedata:raise ValueError('first data extent is not zero')
            if offset is not None and not 8192<=start-offset<=MAX_EXTENT:raise ValueError('invalid page extent')
            if key[0]==1:self.actor_pages+=1
            previous=key;offset=start
        if pages and not 8192<=size-offset<=MAX_EXTENT:raise ValueError('invalid last extent')
        if not pages and size!=pagedata:raise ValueError('empty flat file has page payload')
    def root(self,index):
        if not 0<=index<self.pages:raise ValueError('flat root index out of bounds')
        a,b=struct.unpack_from('<II',self.rootdata,80+4*index)
        if a>b or b>self.pagedata-self.records or b-a<10:raise ValueError('invalid root record extent')
        a+=self.records;b+=self.records
        n=struct.unpack_from('<H',self.rootdata,a)[0]
        if not 1<=n<=MAX_KEY or a+2+n+8!=b:raise ValueError('invalid root key extent')
        key=self.rootdata[a+2:a+2+n]
        if key[0] not in [1,2]:raise ValueError('invalid root namespace')
        offset=struct.unpack_from('<Q',self.rootdata,a+2+n)[0]
        if offset%4096 or offset>self.size-self.pagedata:raise ValueError('unaligned/out-of-range data extent')
        return key,self.pagedata+offset
    def __iter__(self):
        previous=None
        for i in range(self.actor_pages):
            root,start=self.root(i);next_key,end=self.root(i+1) if i+1<self.pages else (None,self.size)
            extent=end-start
            if not 8192<=extent<=MAX_EXTENT:raise ValueError('actor extent exceeds budget')
            page=self.budget.read(self.file,start,extent)
            count,keyend,used,reserved=struct.unpack_from('<IIII',page)
            if not 1<=count<=4096 or (extent>8192 and count!=1) or reserved:raise ValueError('invalid actor page header')
            groups=(count+15)//16;pos=16+2*groups
            if not pos<=keyend<=used<=extent or keyend>65535:raise ValueError('invalid actor page sections')
            prev=b''
            for j in range(count):
                if j%16==0:
                    if struct.unpack_from('<H',page,16+2*(j//16))[0]!=pos:raise ValueError('invalid actor page restart')
                    prev=b''
                shared,pos=varint(page,pos,keyend);suffix,pos=varint(page,pos,keyend)
                if shared>len(prev) or pos+2+suffix>keyend:raise ValueError('invalid front-coded actor key')
                posting=struct.unpack_from('<H',page,pos)[0];pos+=2
                key=prev[:shared]+page[pos:pos+suffix];pos+=suffix
                if not 1<=len(key)<=MAX_KEY or key[0] not in [1,2]:raise ValueError('invalid key namespace/length')
                if (j==0 and key!=root) or (previous is not None and key<=previous):raise ValueError('actor keys not strictly ordered')
                if posting<keyend or posting+4>used:raise ValueError('invalid actor blob offset')
                n=struct.unpack_from('<I',page,posting)[0]
                if posting+4+n>used:raise ValueError('actor blob exceeds page')
                if key[0]==1:
                    did=key[1:].decode('utf8')
                    if not did.startswith('did:') or len(did)<=4:raise ValueError('invalid actor DID')
                    yield did,page[posting+4:posting+4+n]
                previous=prev=key
            if pos!=keyend or (next_key is not None and previous>=next_key):raise ValueError('actor page trailing keys/order mismatch')

def unpack_actor(did,blob,watermark):
    if len(blob)<6 or blob[0]!=1 or blob[1] not in [0,1]:raise ValueError('invalid packed actor header')
    score=struct.unpack_from('<f',blob,2)[0]
    if not math.isfinite(score):raise ValueError('nonfinite packed actor score')
    row={'type':'actor','did':did,'hidden':bool(blob[1]),'deleted':False,'quality_score':score,'quality_score_f32_bits':f'{struct.unpack_from("<I",blob,2)[0]:08x}','version':watermark,'version_kind':'snapshot_watermark','source_updated_at':None,'followers_count':0,'follows_count':0,'posts_count':0,'last_activity_at':0}
    pos=6
    for field in FIELDS:
        if pos+2>len(blob):raise ValueError('truncated packed actor length')
        n=struct.unpack_from('<H',blob,pos)[0];pos+=2
        if pos+n>len(blob):raise ValueError('truncated packed actor field')
        row[field]=blob[pos:pos+n].decode('utf8');pos+=n
    if pos!=len(blob) or not row['handle']:raise ValueError('packed actor trailing bytes or unsearchable handle')
    for field,kind in [('labels',list),('associated',dict)]:
        row[field]=json.loads(row[field])
        if not isinstance(row[field],kind):raise ValueError('invalid hydrated '+field+' JSON shape')
    return row

def run_export(args,stream=None):
    budget=Budget(args);scratch=Path(args.scratch_root).resolve()
    if not scratch.is_dir():raise ValueError('declared cloud scratch root must exist')
    receipt=Path(args.receipt).resolve()
    if not receipt.is_relative_to(scratch) or receipt.exists():raise ValueError('receipt must be new and within declared scratch')
    manifest,prefix,originals=validate_release(args,budget)
    target=manifest['actor_count'] if args.limit_actors is None else min(manifest['actor_count'],args.limit_actors)
    if target<0:raise ValueError('negative actor limit')
    partial=target!=manifest['actor_count'];output=None;temporary=None;out=None
    if args.output!='-':
        output=Path(args.output).resolve()
        if not output.is_relative_to(scratch) or output.exists() or output in originals:raise ValueError('output must be new and within declared scratch')
        temporary=output.with_name(output.name+'.partial')
        out=temporary.open('xb')
    else:out=stream if stream is not None else sys.stdout.buffer
    emitted=0;actors_hash=hashlib.sha256();output_published=False;receipt_published=False;receipt_staged=False
    receipt_temporary=receipt.with_name(receipt.name+'.partial')
    try:
        with prefix.open('rb') as file:
            flat=FlatActors(file,budget,args.max_root_bytes)
            header={'type':'manifest','schema_version':1,'build_id':manifest['active_build'],'watermark':str(manifest['source_watermark']),'source_watermark':manifest['source_watermark'],'actor_count':target,'source_actor_count':manifest['actor_count'],'normalize_version':manifest['normalize_version'],'scoring_version':manifest['scoring_version'],'version_kind':'snapshot_watermark','source_format':'FLATPX05/actor-v1','source_prefix_sha256':manifest['prefix_map']['sha256'],'source_database_sha256':manifest['sha256'],'partial':partial}
            budget.write(out,jsonline(header))
            for did,blob in flat:
                if partial and emitted>=target:break
                if emitted>=target:raise ValueError('more actors than release manifest')
                line=jsonline(unpack_actor(did,blob,manifest['source_watermark']));budget.write(out,line);actors_hash.update(line);emitted+=1
            if emitted!=target:raise ValueError('emitted actor count differs from release manifest')
            for path,before in originals.items():
                if identity(path)!=before:raise ValueError('immutable source changed during export')
            footer={'type':'end','build_id':manifest['active_build'],'actor_count':emitted,'actors_sha256':actors_hash.hexdigest()}
            budget.write(out,jsonline(footer));out.flush()
            if output is not None:
                os.fsync(out.fileno());out.close();out=None
                os.link(temporary,output);output_published=True;temporary.unlink()
        result={'owner':args.owner,'build_id':manifest['active_build'],'source_watermark':manifest['source_watermark'],'source_actor_count':manifest['actor_count'],'emitted_actor_count':emitted,'partial':partial,'actors_sha256':actors_hash.hexdigest(),'output_bytes':budget.output_bytes,'physical_read_bytes':budget.read_bytes,'wall_seconds':time.monotonic()-budget.started,'root_directory_bytes':flat.pagedata,'actor_pages':flat.actor_pages,'max_page_bytes':MAX_EXTENT,'source_prefix_sha256':manifest['prefix_map']['sha256'],'source_database_sha256':manifest['sha256'],'source_unchanged':True,'cloud_local_paths_only':True,'scratch_root':str(scratch),'output':args.output,'version_kind':'snapshot_watermark','per_actor_source_versions_available':False,'input_verification':{'mode':'verified-local-receipt','receipt_sha256':args.verified_inputs_sha256} if getattr(args,'verified_inputs',None) else {'mode':'full-sha256'}}
        with receipt_temporary.open('xb') as receipt_file:
            receipt_staged=True;receipt_file.write(jsonline(result));receipt_file.flush();os.fsync(receipt_file.fileno())
        os.link(receipt_temporary,receipt);receipt_published=True;receipt_temporary.unlink();receipt_staged=False
        return result
    except BaseException:
        if output is not None:
            if out is not None:out.close();out=None
            if temporary is not None:temporary.unlink(missing_ok=True)
            if output_published:output.unlink(missing_ok=True)
        if receipt_staged:receipt_temporary.unlink(missing_ok=True)
        if receipt_published:receipt.unlink(missing_ok=True)
        raise

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['manifest','database','prefix','build-id','scratch-root','output','receipt']:p.add_argument('--'+name,required=True)
    p.add_argument('--owner',default='tantivy-dev-20261028');p.add_argument('--max-seconds',type=float,default=1800)
    p.add_argument('--max-input-bytes',type=int,default=8*1024**3);p.add_argument('--max-output-bytes',type=int,default=16*1024**3)
    p.add_argument('--max-root-bytes',type=int,default=128*1024**2);p.add_argument('--max-actors',type=int,default=20_000_000)
    p.add_argument('--limit-actors',type=int)
    p.add_argument('--verified-inputs');p.add_argument('--verified-inputs-sha256')
    return p
if __name__=='__main__':
    try:
        args=parser().parse_args()
        if not math.isfinite(args.max_seconds) or args.max_seconds<=0:raise ValueError('invalid export wall deadline')
        def expired(*_):raise TimeoutError('export wall deadline exceeded')
        signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,args.max_seconds)
        run_export(args)
    except (ValueError,OSError,KeyError,struct.error,UnicodeError,json.JSONDecodeError,sqlite3.Error) as exc:
        print(json.dumps({'error':'compact_actor_export_failed','kind':type(exc).__name__,'detail':str(exc)}),file=sys.stderr);sys.exit(1)

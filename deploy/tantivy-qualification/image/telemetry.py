"""Bounded read-only Linux resource observations; never reads process environments.

Counters are cumulative, with units and scope explicit. Missing/unreadable data is
None. PID RSS sums can double-count shared pages; cgroup usage is separate. VM and
block-device counters cover the guest, not only selected PIDs. Device rows must
not be summed blindly: parent devices and partitions can overlap. Directory
counts are low-rate bounded observations, never a corpus/request-path scan.
"""
import os
from pathlib import Path
import re
import threading
import time

MAX_PIDS = 32
MAX_READ = 256 * 1024

def read(path, limit=MAX_READ):
    try:
        with Path(path).open('r',encoding='ascii') as stream:
            value=stream.read(limit+1)
        return value if len(value)<=limit else None
    except (OSError,UnicodeError):return None

def integer(value):
    try:return int(value)
    except (ValueError,TypeError):return None

def pairs(value):
    if value is None:return None
    result={}
    for line in value.splitlines():
        columns=line.split()
        if len(columns)==2:result[columns[0]]=integer(columns[1])
    return result

def pressure(path):
    value=read(path,4096)
    if value is None:return None
    result={}
    try:
        for line in value.splitlines():
            columns=line.split();row={}
            for item in columns[1:]:
                key,number=item.split('=');row[key]=int(number) if key=='total' else float(number)
            result[columns[0]]=row
        return result
    except (ValueError,IndexError):return None

def status_memory(path):
    value=read(path,32768)
    if value is None:return None
    keys={'VmRSS','RssAnon','RssFile','RssShmem','VmSize','VmHWM','VmSwap','MemAvailable','Cached','Dirty','Writeback','MemTotal'}
    result={key:None for key in keys}
    for line in value.splitlines():
        columns=line.replace(':','',1).split()
        if len(columns)>=2 and columns[0] in keys:
            result[columns[0]]=integer(columns[1])*1024 if integer(columns[1]) is not None else None
    return result

def mapped_bytes(pid):
    value=read(f'/proc/{pid}/maps')
    if value is None:return None
    rows=value.splitlines()
    if len(rows)>2000:return None
    result={'virtual_bytes':0,'file_virtual_bytes':0,'shared_virtual_bytes':0,'vma_count':len(rows)}
    try:
        for line in rows:
            columns=line.split(None,5);start,end=[int(item,16) for item in columns[0].split('-')];size=end-start
            result['virtual_bytes']+=size
            if int(columns[4])!=0:result['file_virtual_bytes']+=size
            if columns[1][-1]=='s':result['shared_virtual_bytes']+=size
        return result
    except (ValueError,IndexError):return None

def process(pid):
    raw=read(f'/proc/{pid}/stat',8192)
    if raw is None:return {'pid':pid,'present':None if Path(f'/proc/{pid}').exists() else False,'start_ticks':None,'memory_bytes':None,'cpu_ticks':None,'io_bytes':None,'mapped':None,'major_faults':None}
    try:
        # comm can contain spaces/parentheses; fields after its final ')' start at 3.
        fields=raw[raw.rindex(')')+2:].split()
        ticks={'user':int(fields[11]),'system':int(fields[12])}
        start=int(fields[19]);major=int(fields[9])
    except (ValueError,IndexError):return {'pid':pid,'present':None,'start_ticks':None,'memory_bytes':None,'cpu_ticks':None,'io_bytes':None,'mapped':None,'major_faults':None}
    memory=status_memory(f'/proc/{pid}/status')
    statm=read(f'/proc/{pid}/statm',512)
    try: shared=int(statm.split()[2])*os.sysconf('SC_PAGE_SIZE') if statm else None
    except (ValueError,IndexError): shared=None
    if memory is not None:memory['statm_shared_resident_bytes']=shared
    io=pairs(read(f'/proc/{pid}/io',8192))
    # /proc/PID/io keys include ':', unlike cgroup key/value files.
    if io is not None:io={key.rstrip(':'):value for key,value in io.items()}
    after=read(f'/proc/{pid}/stat',8192)
    try:same=after is not None and int(after[after.rindex(')')+2:].split()[19])==start
    except (ValueError,IndexError):same=False
    if not same:return {'pid':pid,'present':False,'start_ticks':None,'memory_bytes':None,'cpu_ticks':None,'io_bytes':None,'mapped':None,'major_faults':None}
    return {'pid':pid,'present':True,'start_ticks':start,'memory_bytes':memory,'cpu_ticks':ticks,'major_faults':major,'io_bytes':None if io is None else {key:io.get(key) for key in ['rchar','wchar','read_bytes','write_bytes','cancelled_write_bytes']},
            'io_syscalls':None if io is None else {key:io.get(key) for key in ['syscr','syscw']},'mapped':mapped_bytes(pid)}

def descendants(pids):
    selected=[];queue=[(pid,0) for pid in pids];complete=True
    while queue:
        pid,depth=queue.pop(0)
        if pid in selected:continue
        if len(selected)>=MAX_PIDS:complete=False;break
        selected.append(pid)
        value=read(f'/proc/{pid}/task/{pid}/children',4096)
        if value is None:
            if Path(f'/proc/{pid}').exists():complete=False
            continue
        children=[integer(item) for item in value.split()]
        if any(child is None or child<=0 for child in children):complete=False;continue
        if children and depth>=4:complete=False;continue
        queue.extend((child,depth+1) for child in children)
    return selected,complete

def diskstats():
    value=read('/proc/diskstats')
    if value is None:return None
    result=[]
    try:
        for line in value.splitlines():
            c=line.split()
            if len(c)<14:continue
            result.append({'major':int(c[0]),'minor':int(c[1]),'device':c[2],
                           'read_bytes':int(c[5])*512,'write_bytes':int(c[9])*512,
                           'io_milliseconds':int(c[12]),'weighted_io_milliseconds':int(c[13])})
        return result
    except (ValueError,IndexError):return None

def cgroup_io():
    value=read('/sys/fs/cgroup/io.stat')
    if value is None:return None
    result={}
    try:
        for line in value.splitlines():
            c=line.split();result[c[0]]={key:int(number) for key,number in [item.split('=') for item in c[1:]]}
        return result
    except (ValueError,IndexError):return None

class Sampler:
    def __init__(self,index_paths=(),directory_interval=30,file_limit=2000):
        if not 10<=directory_interval<=300 or not 1<=file_limit<=2000 or len(index_paths)>4:raise ValueError('telemetry directory bounds')
        self.paths=[Path(path) for path in index_paths]
        if any(not path.is_absolute() for path in self.paths):raise ValueError('telemetry paths must be absolute')
        self.interval=directory_interval;self.limit=file_limit;self.lock=threading.Lock();self.cached=None;self.cached_at=None;self.last=None
    def directory(self,path):
        result={'path':str(path),'bytes':None,'files':None,'segment_groups':None,'merge_temporary_files':None,'bounded_complete':False}
        if path.is_symlink():return result|{'status':'symlink_rejected'}
        try:
            if path.is_file():return result|{'bytes':path.stat().st_size,'files':1,'segment_groups':0,'merge_temporary_files':0,'bounded_complete':True,'status':'ok'}
            if not path.is_dir():return result|{'status':'missing'}
            queue=[(path,0)];count=0;size=0;files=0;temporary=0;segments=set()
            while queue:
                directory,depth=queue.pop(0)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        count+=1
                        if count>self.limit:return result|{'status':'entry_limit'}
                        if entry.is_symlink():return result|{'status':'symlink_rejected'}
                        if entry.is_dir(follow_symlinks=False):
                            if depth>=4:return result|{'status':'depth_limit'}
                            queue.append((Path(entry.path),depth+1))
                        elif entry.is_file(follow_symlinks=False):
                            size+=entry.stat(follow_symlinks=False).st_size;files+=1
                            match=re.match(r'([0-9a-f]{32})\.',entry.name)
                            if match:segments.add(match[1])
                            if entry.name.endswith(('.temp','.tmp')) or 'merge' in entry.name:temporary+=1
            return result|{'bytes':size,'files':files,'segment_groups':len(segments),'merge_temporary_files':temporary,'bounded_complete':True,'status':'ok'}
        except OSError:return result|{'status':'unavailable'}
    def snapshot(self,pids=(),include_descendants=True):
        if len(pids)>MAX_PIDS or any(type(pid) is not int or pid<=0 for pid in pids):raise ValueError('telemetry PID bounds')
        started=time.monotonic();epoch=time.time()
        chosen,complete=descendants(pids) if include_descendants else (list(dict.fromkeys(pids)),True)
        processes=[process(pid) for pid in chosen]
        rss=[(item.get('memory_bytes') or {}).get('VmRSS') for item in processes]
        vm=pairs(read('/proc/vmstat'))
        first=(read('/proc/stat') or '').splitlines()
        cpu=None
        if first and first[0].startswith('cpu '):
            values=[integer(item) for item in first[0].split()[1:]]
            cpu=dict(zip(['user','nice','system','idle','iowait','irq','softirq','steal','guest','guest_nice'],values))
        cg=Path('/sys/fs/cgroup')
        current=integer(read(cg/'memory.current',128));maximum=read(cg/'memory.max',128)
        memory_stat=pairs(read(cg/'memory.stat'));uptime=read('/proc/uptime',128)
        with self.lock:
            actual_interval=None if self.last is None or started<self.last else started-self.last
            self.last=started if self.last is None else max(self.last,started)
            if self.cached_at is None or started-self.cached_at>=self.interval:
                self.cached=[self.directory(path) for path in self.paths];self.cached_at=started
            directories={'observed_monotonic_seconds':self.cached_at,'age_seconds':max(0,time.monotonic()-self.cached_at),
                         'interval_seconds':self.interval,'entry_limit_per_path':self.limit,'paths':self.cached,
                         'merge_file_definition':'name ends .temp/.tmp or contains merge; heuristic, not committed merge count'}
        try:uptime_seconds=float(uptime.split()[0]) if uptime else None
        except (ValueError,IndexError):uptime_seconds=None
        return {'schema_version':1,'epoch_seconds':epoch,'monotonic_seconds':started,'actual_interval_seconds':actual_interval,
                'sample_wall_seconds':time.monotonic()-started,'uptime_seconds':uptime_seconds,
                'psi_units':{'averages':'percent','total':'microseconds'},
                'clock_ticks_per_second':os.sysconf('SC_CLK_TCK'),'processes':processes,'process_selection_complete':complete,
                'summed_process_rss_bytes':sum(rss) if rss and all(value is not None for value in rss) else None,
                'summed_rss_may_double_count_shared_pages':True,'guest_meminfo_bytes':status_memory('/proc/meminfo'),
                'guest_vmstat':None if vm is None else {key:vm.get(key) for key in ['pgmajfault','pswpin','pswpout','oom_kill']},
                'guest_cpu_ticks':cpu,'guest_io_pressure':pressure('/proc/pressure/io'),'guest_memory_pressure':pressure('/proc/pressure/memory'),
                'guest_diskstats':diskstats(),'cgroup':{'scope':str(cg),'memory_current_bytes':current,
                    'memory_max_bytes':integer(maximum),'memory_unlimited':maximum.strip()=='max' if maximum is not None else None,
                    'memory_stat':memory_stat,'memory_events':pairs(read(cg/'memory.events')),
                    'cpu_stat':pairs(read(cg/'cpu.stat')),'cpu_max':read(cg/'cpu.max',128),
                    'io_stat':cgroup_io(),'io_pressure':pressure(cg/'io.pressure'),'memory_pressure':pressure(cg/'memory.pressure')},
                'directories':directories}

import ctypes,mmap,os,resource,tempfile,json,shutil
from pathlib import Path
assert shutil.disk_usage('/tmp').free > 32*1024**3
resource.setrlimit(resource.RLIMIT_DATA,(1024**3,1024**3))
def residency(path):
    size = path.stat().st_size
    pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    if not size:
        return {'pages': 0, 'resident_pages': 0}
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
    libc.mincore.restype = ctypes.c_int
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mmap.restype = ctypes.c_void_p
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.munmap.restype = ctypes.c_int
    with path.open('rb') as source:
        address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_PRIVATE, source.fileno(), 0)
        if address == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), 'read-only private residency mapping failed')
        try:
            vector = (ctypes.c_ubyte * pages)()
            if libc.mincore(address, size, vector):
                raise OSError(ctypes.get_errno(), 'private file residency check failed')
            return {'pages': pages, 'resident_pages': sum(value & 1 for value in vector)}
        finally:
            if libc.munmap(address, size):
                raise OSError(ctypes.get_errno(), 'private residency unmap failed')

with tempfile.TemporaryDirectory(prefix='typeahead-mincore-') as temporary:
 path=Path(temporary)/'sparse-prefix'
 with path.open('wb') as target: target.truncate(7292854272)
 previous_errno=None
 try:
  with path.open('rb') as stream, mmap.mmap(stream.fileno(),0,flags=mmap.MAP_PRIVATE,prot=mmap.PROT_READ|mmap.PROT_WRITE): pass
 except OSError as exc: previous_errno=exc.errno
 assert previous_errno==12, previous_errno
 result=residency(path)
 assert result['pages']>1700000 and result['resident_pages']==0,result
 with path.open('r+b') as stream:
  os.pwrite(stream.fileno(),b'X'*4096,0); os.fsync(stream.fileno())
  hot=residency(path)
  assert hot['resident_pages']>=1,hot
  os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_DONTNEED)
  cold=residency(path)
  assert cold['resident_pages']==0,cold
 print(json.dumps({'previous_writable_mapping_errno':previous_errno,'read_only_mapping':result,'hot':hot,'cold_after_advice':cold,'rlimit_data':1024**3,'payload':'temporary sparse file, retired on exit'}))

import ctypes,mmap,os,tempfile,json
from pathlib import Path
def residency(path, split_at=None):
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
            result = {'pages': pages, 'resident_pages': sum(value & 1 for value in vector)}
            if split_at is not None:
                if not 0 <= split_at <= size or split_at % mmap.PAGESIZE:
                    raise ValueError('invalid metadata/payload split')
                split_page = split_at // mmap.PAGESIZE
                result.update(metadata_pages=split_page,
                    metadata_resident_pages=sum(value & 1 for value in vector[:split_page]),
                    payload_pages=pages - split_page,
                    payload_resident_pages=sum(value & 1 for value in vector[split_page:]))
            return result
        finally:
            if libc.munmap(address, size):
                raise OSError(ctypes.get_errno(), 'private residency unmap failed')

with tempfile.TemporaryDirectory(prefix='typeahead-residency-split-') as temporary:
 path=Path(temporary)/'metadata-and-payload'
 with path.open('w+b') as file:
  file.truncate(8*1024*1024); file.write(b'X'*4096); file.flush();os.fsync(file.fileno())
  os.posix_fadvise(file.fileno(),0,0,os.POSIX_FADV_DONTNEED)
  before=residency(path,4096)
  assert before['resident_pages']==0,before
  os.posix_fadvise(file.fileno(),0,0,os.POSIX_FADV_RANDOM)
  os.pread(file.fileno(),4096,0)
  after=residency(path,4096)
  assert after['metadata_resident_pages']==1 and after['payload_resident_pages']==0,after
  print(json.dumps({'before':before,'after_header_read':after,'payload':'temporary sparse fixture, retired on exit'}))

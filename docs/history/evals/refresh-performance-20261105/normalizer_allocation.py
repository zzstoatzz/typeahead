import ctypes,hashlib,json,time
from pathlib import Path
class Result(ctypes.Structure):
 _fields_=[('data',ctypes.c_void_p),('len',ctypes.c_size_t),('owner',ctypes.c_void_p)]
cases=[json.dumps(v,separators=(',',':')).encode() for v in [
 {'op':'actor','handle':'alice.example','display':'Alice Example'},
 {'op':'actor','handle':'unicode.example','display':'名前日本太郎'},
 {'op':'pds','pds':'https://one.test'},
 {'op':'query','q':'Alice Ex'}]]
results=[];reference=None
for name in ['page','libc','page','libc']:
 lib=ctypes.CDLL('/tmp/typeahead-normalizer-'+name+'.dylib')
 lib.ta_plan.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(Result)];lib.ta_plan.restype=ctypes.c_int
 lib.ta_result_free.argtypes=[ctypes.POINTER(Result)]
 digest=hashlib.sha256();start=time.monotonic()
 for _ in range(10000):
  for raw in cases:
   out=Result();status=lib.ta_plan(raw,len(raw),ctypes.byref(out));assert status==0
   try:digest.update(ctypes.string_at(out.data,out.len))
   finally:lib.ta_result_free(ctypes.byref(out))
 elapsed=time.monotonic()-start
 if reference is None:reference=digest.hexdigest()
 assert digest.hexdigest()==reference
 results.append({'allocator':name,'calls':40000,'elapsed_s':elapsed,'outputs_sha256':digest.hexdigest()})
print(json.dumps({'kind':'local-normalizer-component-microbenchmark','results':results},indent=2))

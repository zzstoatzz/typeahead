import ctypes
import os
from pathlib import Path
import sys


class Stats(ctypes.Structure):
    _fields_ = [
        ("keys", ctypes.c_uint64),
        ("verified_keys", ctypes.c_uint64),
        ("pages", ctypes.c_uint64),
        ("oversized_pages", ctypes.c_uint64),
    ]


def library():
    suffix = "dylib" if sys.platform == "darwin" else "so"
    default = Path(__file__).resolve().parents[2] / "services/zig-out/lib" / f"libtypeahead_compact.{suffix}"
    path = Path(os.environ.get("TYPEAHEAD_COMPACT_LIBRARY", default))
    if not path.is_file():
        raise RuntimeError(f"Native compactor missing: {path}; run just build-compactor")
    lib = ctypes.CDLL(str(path))
    for name in ("typeahead_compact_pack", "typeahead_compact_verify"):
        function = getattr(lib, name)
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Stats)]
        function.restype = ctypes.c_char_p
    return lib


def pack(source, side, scratch):
    stats = Stats()
    error = library().typeahead_compact_pack(
        os.fsencode(source), os.fsencode(side), os.fsencode(scratch), ctypes.byref(stats)
    )
    if error:
        raise ValueError(error.decode())
    return stats


def verify(source, side, digest, stats):
    error = library().typeahead_compact_verify(
        os.fsencode(source), os.fsencode(side), digest.encode("ascii"), ctypes.byref(stats)
    )
    if error:
        raise ValueError(error.decode())
    if stats.verified_keys != stats.keys:
        raise ValueError("native compactor key count mismatch")

import hashlib
import json
import mmap
import os
from pathlib import Path
import sqlite3
import struct
import sys


def varint(b, p):
    n = shift = 0
    while True:
        v = b[p]; p += 1
        n |= (v & 127) << shift
        if v < 128:
            return n, p
        shift += 7
        if shift > 63:
            raise ValueError('invalid varint')


def records(path):
    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        assert m[:8] == b'FLATPX05'
        pages = struct.unpack_from('<I', m, 12)[0]
        roots, data = struct.unpack_from('<QQ', m, 24)
        for i in range(pages):
            r = roots + struct.unpack_from('<I', m, 80 + i * 4)[0]
            size = struct.unpack_from('<H', m, r)[0]
            key = m[r + 2:r + 2 + size]
            if key[0] != 1:
                break
            start = data + struct.unpack_from('<Q', m, r + 2 + size)[0]
            n, end, used = struct.unpack_from('<III', m, start)
            for g in range((n + 15) // 16):
                p = start + struct.unpack_from('<H', m, start + 16 + 2 * g)[0]
                previous = b''
                for _ in range(min(16, n - 16 * g)):
                    shared, p = varint(m, p)
                    suffix, p = varint(m, p)
                    value = struct.unpack_from('<H', m, p)[0]; p += 2
                    key = previous[:shared] + m[p:p + suffix]; p += suffix
                    previous = key
                    if key[0] != 1:
                        continue
                    length = struct.unpack_from('<I', m, start + value)[0]
                    offset = start + value + 4
                    yield key[1:].decode(), m[offset:offset + length], offset


def fields(blob):
    assert blob[0] == 1
    score = struct.unpack_from('<f', blob, 2)[0]
    p = 6; values = []
    for _ in range(7):
        n = struct.unpack_from('<H', blob, p)[0]; p += 2
        values.append(blob[p:p+n].decode()); p += n
    assert p == len(blob)
    return blob[1], score, values


def main():
    snapshot, overlay, output = map(Path, sys.argv[1:])
    output.mkdir(exist_ok=False)
    overlay_rows = {}
    with sqlite3.connect(f'file:{overlay}?mode=ro', uri=True) as db:
        for did, blob in db.execute('SELECT did,blob FROM actor_overlay'):
            overlay_rows[did] = blob
        counts = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ['actor_overlay','prefix_overlay']}
    count = skipped = 0
    digest = hashlib.sha256()
    samples = []
    with (output/'unsorted.tsv').open('w') as out, (output/'overlay-profiles.bin').open('wb') as profiles:
        def emit(did, blob, offset, source):
            nonlocal count, skipped
            hidden, score, values = fields(blob)
            handle, _, _, _, pds, _, _ = values
            handle = handle.lower()
            if hidden or not handle or handle == 'handle.invalid' or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in handle):
                skipped += 1; return
            pds = pds.lower().removeprefix('https://').removeprefix('http://').rstrip('/.')
            line = f'{handle}\t{did}\t{score}\t{pds}\t{offset}\t{len(blob)}\t{source}\n'
            out.write(line); digest.update(line.encode()); count += 1
            if count % 19001 == 0 and len(samples) < 700:
                samples.append({'handle':handle, 'pds':pds})
        for did, blob, offset in records(snapshot):
            if did not in overlay_rows:
                emit(did,blob,offset,0)
        for did, blob in overlay_rows.items():
            if blob is not None:
                offset = profiles.tell(); profiles.write(blob)
                emit(did,blob,offset,1)
    report = {'actors':count,'excluded':skipped,'overlay':counts,'row_sha256':digest.hexdigest(),'samples':samples,'snapshot_bytes':snapshot.stat().st_size}
    (output/'input.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'samples'}),flush=True)


if __name__ == '__main__':
    main()

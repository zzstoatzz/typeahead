import os, struct, bisect, hashlib, sqlite3, shutil, time, json
from pathlib import Path
if not os.environ.get('FLY_APP_NAME', '').startswith('typeahead-startup-'):
    raise RuntimeError('requires an isolated startup experiment app')
start = time.monotonic()
root = Path('/data/max-eval')
root.mkdir(exist_ok=True)
original = Path('/data/index.db.prefix')
small = Path('/data/small-eval-100k/snapshot/index.db.prefix')

def roots(path, tag, complete_only):
    with path.open('rb') as f:
        header = struct.unpack('<8sIIIIQQQQQQQ', f.read(80))
        n = header[2]
        records = header[5]
        data = header[6]
        size = header[7]
        f.seek(0)
        directory = f.read(data)
        ranges = []
        pos = 0
        if complete_only:
            while pos < size:
                try:
                    a = os.lseek(f.fileno(), pos, os.SEEK_DATA)
                except OSError:
                    break
                b = os.lseek(f.fileno(), a, os.SEEK_HOLE)
                ranges.append((a, b))
                pos = b
        else:
            ranges = [(0, size)]
        starts = [a for a, b in ranges]
        entries = []
        for i in range(n):
            r = records + struct.unpack_from('<I', directory, 80 + 4 * i)[0]
            length = struct.unpack_from('<H', directory, r)[0]
            key = directory[r + 2:r + 2 + length]
            if key[0] != tag:
                continue
            off = data + struct.unpack_from('<Q', directory, r + 2 + length)[0]
            if i + 1 < n:
                nr = records + struct.unpack_from('<I', directory, 80 + 4 * (i + 1))[0]
                nl = struct.unpack_from('<H', directory, nr)[0]
                end = data + struct.unpack_from('<Q', directory, nr + 2 + nl)[0]
            else:
                end = size
            region = bisect.bisect_right(starts, off) - 1
            if region >= 0 and end <= ranges[region][1]:
                entries.append((key, off, end - off, tag))
        return entries
actors = roots(original, 1, True)
prefixes = roots(small, 2, False)
entries = actors + prefixes
assert actors and prefixes and all((a[0] < b[0] for a, b in zip(entries, entries[1:])))
print('Complete reusable pages:', len(actors), 'actor pages,', len(prefixes), 'prefix pages; payload bytes', sum((e[2] for e in entries)), flush=True)
records = 80 + 4 * (len(entries) + 1)
directory = bytearray()
offsets = []
payload = 0
for key, off, length, tag in entries:
    offsets.append(len(directory))
    directory += struct.pack('<H', len(key)) + key + struct.pack('<Q', payload)
    payload += length
offsets.append(len(directory))
data = (records + len(directory) + 4095) // 4096 * 4096
size = data + payload
header = struct.pack('<8sIIIIQQQQQQQ', b'FLATPX05', 5, len(entries), 0, 0, records, data, size, 0, 0, 0, 0)
initial = header + struct.pack('<' + 'I' * len(offsets), *offsets) + directory
initial += bytes(data - len(initial))
h = hashlib.sha256()
h.update(initial)
actor_count = 0
prefix_count = 0
written = 0
with original.open('rb', buffering=4 * 1024 * 1024) as a, small.open('rb', buffering=1024 * 1024) as p, (root / 'index.db.prefix').open('wb', buffering=4 * 1024 * 1024) as out:
    out.write(initial)
    for key, off, length, tag in entries:
        source = a if tag == 1 else p
        if source.tell() != off:
            source.seek(off)
        page = source.read(length)
        assert len(page) == length
        count = struct.unpack_from('<I', page)[0]
        assert 0 < count <= 4096
        if tag == 1:
            actor_count += count
        else:
            prefix_count += count
        h.update(page)
        out.write(page)
        written += length
        if written // (256 * 1024 * 1024) != (written - length) // (256 * 1024 * 1024):
            print('Copied', written, 'bytes;', actor_count, 'actors;', round(time.monotonic() - start, 1), 'seconds', flush=True)
    out.flush()
    os.fsync(out.fileno())
shutil.copyfile('/data/small-eval-100k/snapshot/index.db', root / 'index.db')
with sqlite3.connect(root / 'index.db') as db:
    db.executemany('INSERT OR REPLACE INTO index_meta VALUES(?,?)', [('flat_prefix_sha256', h.hexdigest()), ('actor_count', str(actor_count)), ('prefix_count', str(prefix_count)), ('build_id', 'downloaded-actor-subset')])
manifest = {'actor_count': actor_count, 'prefix_count': prefix_count, 'bytes': size, 'sha256': h.hexdigest(), 'preparation_seconds': time.monotonic() - start, 'source': 'all complete downloaded actor extents plus regenerated 100k-actor prefix pages'}
(root / 'subset.json').write_text(json.dumps(manifest, indent=2))
sqlite3.connect(root / 'overlay.db').close()
print('Subset ready:', json.dumps(manifest), flush=True)

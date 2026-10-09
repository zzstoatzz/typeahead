import struct

MAGIC = b"FLATPX05"
HEADER = struct.Struct("<8sIIIIQQQQQQQ")
MAX_BLOCK = 8193


def read_varint(data, pos, end):
    value = 0
    for shift in (0, 7, 14):
        if pos >= end:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value > MAX_BLOCK:
                raise ValueError("length overflow")
            return value, pos
    raise ValueError("varint overflow")


def iter_new(data):
    magic, version, pages, r1, r2, roots, pagedata, size, *reserved = (
        HEADER.unpack_from(data)
    )
    if (
        magic != MAGIC
        or version != 5
        or r1
        or r2
        or any(reserved)
        or size != len(data)
        or roots != 80 + 4 * (pages + 1)
    ):
        raise ValueError("header")
    previous = None
    for i in range(pages):
        a, b = struct.unpack_from("<II", data, 80 + 4 * i)
        a += roots
        b += roots
        n = struct.unpack_from("<H", data, a)[0]
        if a + 2 + n + 8 != b:
            raise ValueError("root")
        first = bytes(data[a + 2 : a + 2 + n])
        start = pagedata + struct.unpack_from("<Q", data, a + 2 + n)[0]
        count, keyend, used, reserved = struct.unpack_from("<IIII", data, start)
        groups = (count + 15) // 16
        pos = start + 16 + 2 * groups
        prev = b""
        if not count or reserved or not pos <= start + keyend <= start + used <= size:
            raise ValueError("page")
        for j in range(count):
            if j % 16 == 0:
                prev = b""
                if (
                    struct.unpack_from("<H", data, start + 16 + 2 * (j // 16))[0]
                    != pos - start
                ):
                    raise ValueError("restart")
            shared, pos = read_varint(data, pos, start + keyend)
            suffix, pos = read_varint(data, pos, start + keyend)
            if shared > len(prev) or pos + 2 + suffix > start + keyend:
                raise ValueError("entry")
            posting = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            key = prev[:shared] + data[pos : pos + suffix]
            pos += suffix
            if len(key) > MAX_BLOCK or not key or key[0] not in (1, 2):
                raise ValueError("key namespace/length")
            if posting < keyend or posting + 4 > used:
                raise ValueError("posting offset")
            length = struct.unpack_from("<I", data, start + posting)[0]
            if posting + 4 + length > used:
                raise ValueError("posting length")
            if (j == 0 and key != first) or (previous is not None and key <= previous):
                raise ValueError("order")
            yield (
                bytes(key),
                bytes(data[start + posting + 4 : start + posting + 4 + length]),
            )
            previous = prev = key
        if pos != start + keyend:
            raise ValueError("key trailing")

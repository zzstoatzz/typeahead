import json


def iter_json_values(fh, chunk=1 << 20):
    """Yield JSON values from a stream of them, JSONL **or** concatenated.

    The two bundle sources disagree on format and the filename lies about it:
      - plc.t3.storage.dev publishes true JSONL (newline per op)
      - `allegedly bundle` writes serde_json::to_string(&op) with NO separator
        (weekly.rs:172), so a whole week is ONE ~950MB "line" of back-to-back
        objects — still named .jsonl.gz

    Line-based reading therefore worked on 149 published bundles and failed on
    all 26 allegedly-written ones ("Extra data: line 1 column 706"). raw_decode
    handles both, and reading in chunks means a 950MB single-line file never
    has to be materialized as one string.
    """
    dec = json.JSONDecoder()
    buf = ""
    while True:
        data = fh.read(chunk)
        if not data:
            break
        buf += data
        idx = 0
        while True:
            while idx < len(buf) and buf[idx].isspace():
                idx += 1
            if idx >= len(buf):
                break
            try:
                obj, end = dec.raw_decode(buf, idx)
            except ValueError:
                break  # object straddles the chunk boundary — wait for more
            yield obj
            idx = end
        buf = buf[idx:]
    idx = 0
    while idx < len(buf):
        while idx < len(buf) and buf[idx].isspace():
            idx += 1
        if idx >= len(buf):
            break
        obj, end = dec.raw_decode(buf, idx)
        yield obj
        idx = end

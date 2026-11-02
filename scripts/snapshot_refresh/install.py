"""Helpers tantivy-slot.py imports; the fixed slot image ships this module as /app/install.py."""
import json
import os
import re


def object_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value):
        raise ValueError('Invalid artifact key')
    if any(p in ('.', '..') for p in value.split('/')):
        raise ValueError('Invalid artifact path')
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


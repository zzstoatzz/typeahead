#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest"]
# ///
"""Regression tests for the dump parser in plc-identity-sync.py.

Both cases here are bugs that actually shipped into a run on 2026-07-28 and
would have corrupted data silently rather than failing.
"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_loader("plcsync", loader=None)
mod = importlib.util.module_from_spec(spec)
exec(Path(__file__).with_name("plc-identity-sync.py").read_text(), mod.__dict__)
sys.modules["plcsync"] = mod


def test_trailing_comment_does_not_swallow_the_next_column():
    """A line comment runs to end-of-line. Stripping it per comma-segment lost
    `updated_at`, shifting every later index by one — which made `pds` resolve to
    `hidden`. Wrong column, written back as if it were a PDS endpoint."""
    ddl = """CREATE TABLE IF NOT EXISTS actors (
  did TEXT PRIMARY KEY,
  handle TEXT NOT NULL DEFAULT '',
  avatar_url TEXT DEFAULT '',  -- stores CID only (e.g. bafkrei...); reconstruct later
  updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
  hidden INTEGER NOT NULL DEFAULT 0
, pds TEXT DEFAULT '', labels TEXT NOT NULL DEFAULT '[]');"""
    cols = mod._parse_columns(ddl)
    assert cols == ["did", "handle", "avatar_url", "updated_at", "hidden", "pds", "labels"]
    assert cols.index("pds") == 5


def test_parenthesised_defaults_do_not_break_splitting():
    ddl = "CREATE TABLE x (a TEXT, b INTEGER DEFAULT (unixepoch()), c TEXT);"
    assert mod._parse_columns(ddl) == ["a", "b", "c"]


def test_table_constraints_are_not_columns():
    ddl = "CREATE TABLE x (a TEXT, b TEXT, PRIMARY KEY (a, b));"
    assert mod._parse_columns(ddl) == ["a", "b"]


def test_split_values_handles_escaped_quotes():
    """Handles legitimately contain apostrophes; SQLite escapes them as ''.
    Mis-splitting there truncates a handle and we would store the fragment."""
    row = "'did:plc:abc','o''brien.example.com','O''Brien',''"
    assert mod._split_sql_values(row) == [
        "did:plc:abc", "o'brien.example.com", "O'Brien", "",
    ]


def test_split_values_stops_at_close_paren():
    row = "'did:plc:abc','h.example',123);"
    assert mod._split_sql_values(row)[:3] == ["did:plc:abc", "h.example", "123"]

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).with_name('reconcile-ingest-recovery.py'))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def seed(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE audit(did TEXT PRIMARY KEY, present INTEGER);
          CREATE TABLE repo_status(did TEXT PRIMARY KEY, active INTEGER);
          CREATE TABLE profile_available(did TEXT PRIMARY KEY, available INTEGER);
          INSERT INTO audit VALUES ('did:plc:example',0);
          INSERT INTO repo_status VALUES ('did:plc:example',1);
          INSERT INTO profile_available VALUES ('did:plc:example',1);
        ''')


def test_health_failure_prevents_network_mutations(tmp_path):
    path = tmp_path / 'audit.sqlite'
    seed(path)
    with patch.object(recovery, 'load_health_check', return_value=lambda client: False), \
         patch.object(Path, 'read_text', return_value='TURSO_URL=libsql://example\nTURSO_AUTH_TOKEN=test\nADMIN_SECRET=test'), \
         pytest.raises(RuntimeError, match='no writes'):
        recovery.main(SimpleNamespace(audit=str(path), limit=5, apply=True))
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from restoration').fetchone()[0] == 0


def test_uncertain_insert_resumes_without_reinserting_or_advancing_cursor(tmp_path):
    path = tmp_path / 'audit.sqlite'
    seed(path)
    inserted = False
    inserts = 0

    def handler(request):
        nonlocal inserted, inserts
        if request.url.host == 'example':
            rows = [[{'value': 'did:plc:example'}]] if inserted else []
            return httpx.Response(200, json={'results': [{'type': 'ok', 'response': {'result': {'rows': rows}}}]})
        if request.url.host == 'bsky.network':
            assert 'Authorization' not in request.headers
            return httpx.Response(200, json={'active': True})
        if request.url.path == '/admin/ingest':
            assert 'cursor' not in json.loads(request.content)
            inserts += 1
            inserted = True
            raise httpx.ReadTimeout('response lost after write')
        if request.url.path == '/admin/enrich-profiles':
            return httpx.Response(200, json={'written': 1, 'errors': []})
        raise AssertionError(str(request.url))

    original = httpx.Client
    with patch.object(recovery, 'load_health_check', return_value=lambda client: True), \
         patch.object(Path, 'read_text', return_value='TURSO_URL=libsql://example\nTURSO_AUTH_TOKEN=test\nADMIN_SECRET=test'), \
         patch.object(recovery.httpx, 'Client', side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
        args = SimpleNamespace(audit=str(path), limit=5, apply=True)
        with pytest.raises(httpx.ReadTimeout):
            recovery.main(args)
        with sqlite3.connect(path) as db:
            assert db.execute('select state from restoration').fetchone()[0] == 'restore_pending'
        recovery.main(args)
    assert inserts == 1
    with sqlite3.connect(path) as db:
        assert db.execute('select state from restoration').fetchone()[0] == 'enriched'

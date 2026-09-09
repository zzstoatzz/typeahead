import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

spec = importlib.util.spec_from_file_location('refresh', Path(__file__).with_name('refresh-recovery-profiles.py'))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


@pytest.mark.parametrize('active,expected_writes', [(False,0),(True,1)])
def test_only_active_existing_bsky_profiles_reach_canonical_writer(tmp_path, active, expected_writes):
    path=tmp_path/'audit.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE profile_comparison(did TEXT,status TEXT); INSERT INTO profile_comparison VALUES ('did:plc:test','differs');")
    writes=[]
    def handler(request):
        if request.url.host=='example':
            return httpx.Response(200,json={'results':[{'type':'ok','response':{'result':{'rows':[[{'value':'did:plc:test'},{'value':'bsky'}]]}}}]})
        if request.url.host=='bsky.network':
            assert 'Authorization' not in request.headers
            return httpx.Response(200,json={'active':active})
        assert request.url.path=='/admin/enrich-profiles'
        body=json.loads(request.content)
        assert body=={'dids':['did:plc:test']}
        writes.append(body)
        return httpx.Response(200,json={'written':1,'errors':[]})
    original=httpx.Client
    with patch.object(refresh.runpy,'run_path',return_value={'serving_healthy':lambda client:True}), \
         patch.object(Path,'read_text',return_value='TURSO_URL=libsql://example\nTURSO_AUTH_TOKEN=test\nADMIN_SECRET=test'), \
         patch.object(refresh.httpx,'Client',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs)):
        refresh.main(SimpleNamespace(audit=str(path),limit=25,apply=True))
    assert len(writes)==expected_writes
    with sqlite3.connect(path) as db:
        assert db.execute('select state from profile_refresh').fetchone()[0] == ('refreshed_needs_verification' if active else 'inactive_review')

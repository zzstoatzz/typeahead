import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from smoke import BIN, actor, request, start, stop, write_seed


def run():
    previous = {name: os.environ.get(name) for name in ['FLY_MACHINE_ID', 'FLY_MACHINE_VERSION']}
    checks = 0
    try:
        with tempfile.TemporaryDirectory(prefix='tantivy-runtime-identity-') as temporary:
            root = Path(temporary)
            seed, data = root/'seed.jsonl', root/'data'
            write_seed(seed, [actor(1, 'runtime.test', 'Runtime Identity')])
            subprocess.run([str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'], check=True, capture_output=True, timeout=30)
            for machine, version in [(None, None), ('fixture-machine-01', 'FIXTURE-VERSION-01')]:
                for name, value in [('FLY_MACHINE_ID', machine), ('FLY_MACHINE_VERSION', version)]:
                    if value is None:os.environ.pop(name, None)
                    else:os.environ[name] = value
                with (root/'server.log').open('ab') as log:
                    child, public, _admin = start(data, log, secrets.token_hex(16))
                    try:
                        for endpoint in ['/health', '/ready', '/index/status', '/index-status', '/search?q=Runtime', '/debug/search?q=Runtime', '/xrpc/tech.waow.typeahead.searchActors?q=Runtime', '/xrpc/app.bsky.actor.searchActorsTypeahead?q=Runtime']:
                            code, headers, body = request(public, endpoint)
                            assert code == 200
                            assert headers.get('x-typeahead-machine-id') == machine
                            assert headers.get('x-typeahead-machine-version') == version
                            assert headers['x-typeahead-binary-sha256'] == hashlib.sha256(BIN.read_bytes()).hexdigest()
                            if endpoint == '/index/status':assert body['machine_id'] == machine and body['instance_id'] == version
                            checks += 1
                    finally:stop(child)
            for name, invalid in [('FLY_MACHINE_ID',''), ('FLY_MACHINE_ID','a'*129), ('FLY_MACHINE_ID','non-ascii-ü'), ('FLY_MACHINE_VERSION','bad\r\nheader')]:
                env=dict(os.environ)
                env[name]=invalid
                result=subprocess.run([str(BIN),'serve','--data',str(data),'--allow-partial','true','--allow-stale','true','--listen','127.0.0.1:0','--admin-listen','127.0.0.1:0'],env=env,capture_output=True,text=True,timeout=5)
                assert result.returncode != 0 and 'invalid provider runtime identity' in result.stderr
                assert invalid not in result.stderr if invalid else True
                checks += 1
        return {'passed':True,'checks':checks,'actual_startup_environment_read':True,'local_missing_identity_is_null':True,'invalid_runtime_metadata_rejected':True,'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest(),'production_contact':False,'payload_removed':True}
    finally:
        for name, value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value


if __name__ == '__main__':
    result=run()
    target=Path(os.environ.get('TA_RUNTIME_IDENTITY_RECEIPT',Path(__file__).resolve().parent.parent/'runtime-identity-receipt.json'))
    target.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))

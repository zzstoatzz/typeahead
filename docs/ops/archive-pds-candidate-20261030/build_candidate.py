"""Run the existing checkpoint builder on cloud scratch, without promotion."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import boto3
from mps.typeahead_checkpoint import fetch_checkpoint
from mps.typeahead_checkpoint_build import build_checkpoint
from mps.typeahead_checkpoint_stream import file_witness
from mps.typeahead_tantivy import TantivyAdapter, durable_receipt


def main():
    root = Path('/tmp/typeahead-pds-join-full')
    if sys.platform != 'linux' or Path.cwd() != root:
        raise ValueError('Cloud scratch directory required')
    credentials = json.load(sys.stdin)
    started = time.monotonic()
    repository = root / 'code/typeahead'
    binary = root / 'code/typeahead-tantivy'
    binary_sha = hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest()
    if binary_sha != 'd1cc0366ed3b28b34daca15d23ee6f06aa1d08844e483c357b80cdb778d5cfa2':
        raise ValueError('Unexpected reader binary')
    client = boto3.client('s3', endpoint_url=credentials['INDEX_R2_ENDPOINT'],
                          aws_access_key_id=credentials['INDEX_R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=credentials['INDEX_R2_SECRET_ACCESS_KEY'])
    bucket = credentials['INDEX_R2_BUCKET']
    def read(key):
        response = client.get_object(Bucket=bucket, Key=key)
        with response['Body'] as stream:
            value = stream.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise ValueError('Metadata exceeded bound')
        return value
    route = read('control/search-routing.json')
    routing = json.loads(route)
    target = routing.get('native_rollback')
    if not isinstance(target, dict):
        raise ValueError('Retained native anchor required')
    anchor = read(f"releases/search/{target['build_id']}/manifest.json")
    if hashlib.sha256(anchor).hexdigest() != 'de5d4e3a9385767bff203c2e06ffd5d400400f685d4e3f926a88af3abfdc2d4c':
        raise ValueError('Native anchor changed; inspect before building')
    (root / 'native-manifest.json').write_bytes(anchor)
    (root / 'routing-before.json').write_bytes(route)
    mapping = json.loads((root / 'mapping-receipt.json').read_bytes())
    def progress(event):
        print(json.dumps(event), flush=True)
    if (root / 'checkpoint.json').exists():
        raw = (root / 'checkpoint.json').read_bytes()
        checkpoint = json.loads(raw)
    else:
        checkpoint = fetch_checkpoint(credentials['TURSO_URL'], credentials['TURSO_AUTH_TOKEN'],
                                      root / 'source.db', max_seconds=900, progress=progress)
        raw = json.dumps(checkpoint, sort_keys=True).encode() + b'\n'
        (root / 'checkpoint.json').write_bytes(raw)
    build = f"b{checkpoint['source_clock_before']}-cp-pdsjoin"
    environment = dict(os.environ, TURSO_URL=credentials['TURSO_URL'],
                       TURSO_AUTH_TOKEN=credentials['TURSO_AUTH_TOKEN'],
                       TA_RETRIEVAL_STRATEGY='global-merge')
    imported = build_checkpoint(repository, root / 'build', binary, root / 'source.db',
        receipt_bytes=raw, receipt_sha256=hashlib.sha256(raw).hexdigest(),
        expected_source_origin_sha256=checkpoint['source_origin_sha256'],
        expected_file_witness=file_witness(root / 'source.db'),
        native_manifest_bytes=anchor, native_manifest_sha256=hashlib.sha256(anchor).hexdigest(),
        build_id=build, environment=environment, timeout=3600,
        pds_fallback_path=root / 'pds-fallback.sqlite', pds_fallback_sha256=mapping['sha256'])
    durable_receipt(root / 'import-result.json', imported)
    installer = repository / 'scripts/snapshot_refresh/tantivy_install.py'
    adapter = TantivyAdapter(installer, hashlib.sha256(installer.read_bytes()).hexdigest())
    manifest = dict(imported['manifest_fields'])
    manifest.update(engine='tantivy', manifest_version=1, artifact_format='tantivy-directory-tar-v1',
        backend_format=adapter.installer.FORMAT, normalize_version='v2-cjk-bigram',
        plan_version='complete-prefix-groups-v1', active_build=build, binary_sha256=binary_sha,
        retrieval_strategy='global-merge', sha256='0'*64, byte_size=1,
        object_key=f'releases/search-tantivy/{build}/index.tar',
        files=[{'path':'index/meta.json','byte_size':1,'sha256':'0'*64},
               {key:manifest['pds_fallback'][key] for key in ('path','byte_size','sha256')}])
    portable = adapter.freeze(root / 'build/data', root / 'publication', manifest, binary, anchor)
    result = dict(build_id=build, seconds=time.monotonic()-started,
                  checkpoint_seconds=checkpoint.get('wall_seconds'),
                  import_seconds=imported['wall_seconds'], mapping=mapping,
                  coverage=imported['stream']['pds_coverage'], bundle_bytes=portable['byte_size'],
                  source_bytes=(root/'source.db').stat().st_size,
                  production_routing_unchanged=json.loads(read('control/search-routing.json'))['current'] == routing['current'],
                  promotion_permitted=False)
    durable_receipt(root / 'candidate-result.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'failed':type(error).__name__}), flush=True)
        raise SystemExit(1) from None

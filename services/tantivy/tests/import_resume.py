import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile

from smoke import BIN, actor, write_seed


def invoke(*args, crash=False):
    env = dict(os.environ)
    if crash:
        env['TYPEAHEAD_TEST_CRASH'] = 'import_after_footer'
    else:
        env.pop('TYPEAHEAD_TEST_CRASH', None)
    return subprocess.run([str(BIN), *map(str, args)], env=env, capture_output=True,
                          text=True, timeout=60,
                          preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))


with tempfile.TemporaryDirectory(prefix='tantivy-import-resume-') as directory:
    root = Path(directory)
    seed = root / 'seed.jsonl'
    rows = [actor(i, f'person{i}.test', f'Person {i}') for i in range(100)]
    write_seed(seed, rows)
    lines = seed.read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0]); header['partial'] = False
    header['source_bootstrap'] = {'checkpoint_receipt': {'wall_seconds': 0.025235401466488838}}
    lines[0] = json.dumps(header).encode() + b'\n'
    seed.write_bytes(b''.join(lines))
    footer = json.loads(lines[-1])
    receipt = root / 'receipt.json'
    value = {'source_manifest': header, 'input_documents': len(rows),
             'actors_sha256': footer['actors_sha256']}
    receipt.write_text(json.dumps(value))
    def resume(data):
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        return invoke('resume-import', '--data', data, '--receipt', receipt,
                      '--receipt-sha256', digest)
    data = root / 'interrupted'
    result = invoke('import', '--data', data, '--input', seed, '--batch-size', 17, crash=True)
    assert result.returncode != 0
    before = json.loads((data / 'index/meta.json').read_text())['payload']
    assert not json.loads(before)['import_complete']
    wrong = dict(value, actors_sha256='0' * 64)
    receipt.write_text(json.dumps(wrong))
    assert resume(data).returncode != 0
    assert json.loads((data / 'index/meta.json').read_text())['payload'] == before
    receipt.write_text(json.dumps(value))
    result = resume(data)
    assert result.returncode == 0, result.stderr
    completed = json.loads(result.stdout)
    assert completed['source_manifest'] == header
    assert completed['import_complete'] and completed['input_actors'] == len(rows)
    assert completed['expiry_coverage']['import_complete']
    assert json.loads(resume(data).stdout) == completed
    truncated = root / 'truncated.jsonl'
    truncated.write_bytes(b''.join(lines[:-1]))
    unfinished = root / 'unfinished'
    assert invoke('import', '--data', unfinished, '--input', truncated, '--batch-size', 17).returncode != 0
    assert resume(unfinished).returncode != 0
    assert invoke('resume-import', '--data', data, '--receipt', receipt,
                  '--receipt-sha256', '0' * 64).returncode != 0
    print('PASS: interrupted verified import resumes; wrong receipt, missing footer and wrong checksum rejected; completion is idempotent')

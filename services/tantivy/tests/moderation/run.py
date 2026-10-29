"""Actual Bun/Rust moderation differential runner, with bounded local subprocesses."""
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]

def run(argv):
    result = subprocess.run(argv, cwd=REPO, text=True, capture_output=True, timeout=360)
    if result.returncode:
        raise RuntimeError(result.stdout[-5000:] + result.stderr[-5000:])
    return result.stdout

def main():
    run(['bun','scripts/export-moderation-regexes.ts','--check'])
    run(['bun','services/tantivy/tests/moderation/generate.ts'])
    output=run(['cargo','test','--offline','--release','--locked','--manifest-path','services/tantivy/Cargo.toml',
                '--test','moderation_differential','--','--nocapture'])
    row=next(line for line in output.splitlines() if line.startswith('MODERATION_DIFFERENTIAL '))
    report=json.loads(row.removeprefix('MODERATION_DIFFERENTIAL '))
    report.update({'passed':True,'kind':'actual-Bun-Rust-moderation-differential','cloud_calls':0,
        'canonical_source_matches_artifact':True,
        'fixture_sha256':hashlib.sha256((HERE/'cases.json').read_bytes()).hexdigest(),
        'runtime_module_sha256':hashlib.sha256((REPO/'services/tantivy/src/moderation.rs').read_bytes()).hexdigest(),
        'limitations':['Unsupported expiry formats fail closed; source format coverage remains required before cutover.',
                       'This synthetic differential fixture is not source-wide expiry coverage or deployed qualification.']})
    (HERE/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':main()

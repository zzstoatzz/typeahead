import hashlib
import json
from pathlib import Path
import subprocess
import sys

output, mps = map(Path, sys.argv[1:3])
output.mkdir(parents=True, exist_ok=False)
repository = Path(__file__).resolve().parents[3]

def revision(repo):
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()

typeahead_revision, mps_revision = revision(repository), revision(mps)

def copy(repo, commit, source, destination):
    target = output/destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(subprocess.check_output(['git', '-C', str(repo), 'show', commit+':'+source]))

for name in ['tantivy-slot.py', 'tantivy_install.py', 'install.py',
             'test_tantivy_slot.py', 'test_tantivy_install.py']:
    source = 'scripts/snapshot_refresh/'+name
    copy(repository, typeahead_revision, source, 'qualification/typeahead/'+source)
paths = subprocess.check_output(['git', '-C', str(repository), 'ls-tree', '-r',
    '--name-only', typeahead_revision, 'services/tantivy-input']).decode().splitlines()
for source in paths:
    if source.endswith('.py'):
        copy(repository, typeahead_revision, source, 'qualification/typeahead/'+source)
copy(repository, typeahead_revision, 'services/tantivy/tests/smoke.py',
    'qualification/typeahead/services/tantivy/tests/smoke.py')
for source in ['src/moderation.ts', 'src/moderation-regexes.json',
               'scripts/snapshot_compaction/publish.py']:
    copy(repository, typeahead_revision, source, 'qualification/typeahead/'+source)
for name in ['typeahead_checkpoint', 'typeahead_checkpoint_build', 'typeahead_checkpoint_stream',
             'typeahead_checkpoint_release', 'typeahead_sqlite_seed',
             'typeahead_tantivy', 'typeahead_source_budget']:
    source = 'packages/mps/src/mps/'+name+'.py'
    copy(mps, mps_revision, source, 'qualification/mps/'+source)
(output/'qualification/mps/packages/mps/src/mps/__init__.py').write_text('')
copy(mps, mps_revision, 'tests/test_typeahead_checkpoint_build.py',
    'qualification/mps/tests/test_typeahead_checkpoint_build.py')
(output/'qualification/run-fixtures.py').write_bytes(Path(__file__).with_name('run-fixtures.py').read_bytes())
(output/'Dockerfile').write_text('''FROM registry.fly.io/typeahead-tantivy-b@sha256:bc66ce9eec3aca5d568135e6d0637f2e2be6e0c305095fc29e4fd50c15d0d34d AS runtime
COPY qualification/typeahead/scripts/snapshot_refresh/tantivy-slot.py /app/tantivy-slot.py
COPY qualification/typeahead/scripts/snapshot_refresh/tantivy_install.py /app/tantivy_install.py
FROM runtime AS fixtures
COPY qualification /qualification
ENTRYPOINT ["python3", "/qualification/run-fixtures.py"]
''')
files = {str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
    for path in output.rglob('*') if path.is_file()}
manifest = dict(typeahead_revision=typeahead_revision, mps_revision=mps_revision,
    base_image='registry.fly.io/typeahead-tantivy-b@sha256:bc66ce9eec3aca5d568135e6d0637f2e2be6e0c305095fc29e4fd50c15d0d34d',
    files=files, source_bytes=sum(path.stat().st_size for path in output.rglob('*') if path.is_file()))
assert manifest['source_bytes'] < 2*1024**2
(output/'package.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(json.dumps({key:value for key,value in manifest.items() if key!='files'}))

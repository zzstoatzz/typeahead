from pathlib import Path
import subprocess

store = Path.home() / 'tangled.org/zzstoatzz.io/secrets/prod.yaml'
result = subprocess.run(
    ['sops', '--decrypt', '--extract', '["logfire"]["typeahead-write-token"]', str(store)],
    capture_output=True, text=True,
)
if result.returncode or not result.stdout.strip():
    raise SystemExit('Cannot load the encrypted Typeahead telemetry credential')
token = result.stdout.strip()
if any(c in token for c in '\r\n\0'):
    raise SystemExit('Invalid telemetry credential format')
for app in ('typeahead-startup-20261024', 'typeahead-search-refresh'):
    result = subprocess.run(
        ['fly', 'secrets', 'import', '--stage', '-a', app],
        input='LOGFIRE_TOKEN=' + token + '\n', capture_output=True, text=True,
    )
    if result.returncode:
        raise SystemExit('Failed to stage telemetry for ' + app)
    print(app + ': telemetry credential staged; running machines unchanged')

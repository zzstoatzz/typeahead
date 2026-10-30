import json,secrets,subprocess
from pathlib import Path
import boto3
ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
APP='typeahead-tantivy-dev'
source=json.loads((REPO/'docs/evals/tantivy-dev-20261028/source-manifest.json').read_text())
s=json.loads(subprocess.check_output(['sops','decrypt','--output-type','json',str(Path.home()/'tangled.org/zzstoatzz.io/secrets/prod.yaml')],text=True))['prefect']['blocks']
client=boto3.client('s3',endpoint_url=s['index-r2-endpoint'],aws_access_key_id=s['index-r2-access-key-id'],aws_secret_access_key=s['index-r2-secret-access-key'],region_name='auto')
def capability(key):return client.generate_presigned_url('get_object',Params={'Bucket':s['index-r2-bucket'],'Key':key},ExpiresIn=14400)
access={'build_id':source['active_build'],'database':capability(source['snapshot_url']),'prefix':capability(source['prefix_map']['object_key'])}
values={'TANTIVY_SOURCE_ACCESS':json.dumps(access,separators=(',',':')),'ADMIN_SECRET':secrets.token_urlsafe(40),'TURSO_URL':s['typeahead-turso-url'],'TURSO_AUTH_TOKEN':s['typeahead-turso-token']}
assert all(not any(c in v for c in '\r\n\0') for v in values.values())
result=subprocess.run(['fly','secrets','import','--stage','-a',APP],input='\n'.join(k+'='+v for k,v in values.items())+'\n',text=True,capture_output=True)
if result.returncode:raise RuntimeError('dev secret installation failed')
print(json.dumps({'app':APP,'runtime_secret_names':sorted(values),'r2_access':'GET-only capabilities for two pinned objects,4h expiry','source_credential':'canonical existing Typeahead database credential; replay code permits fixed SELECTs only','upstream_database_writes':False}))

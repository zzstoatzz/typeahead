import argparse
import copy
import json
import os
from pathlib import Path
import time
import urllib.request
from uuid import UUID,uuid4

import boto3
from botocore.exceptions import ClientError
from prefect.blocks.system import Secret
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter,FlowRunFilterTags
from prefect.exceptions import ObjectNotFound

DEPLOYMENT = UUID('a0c0ce9c-5bab-4bdc-8512-39984c322a74')
PUBLISHER = '2daeb97f825c1ad507dccc0020f28ead665f8350'
SOURCE = '7d732639d4d63b1fe0abaeb4b6f07224640d1189'
INSTALLER = 'cf893cb984e97453a1807e1de2d52d096664525636829884d734f42ab191f26b'
IMAGE = 'registry.fly.io/typeahead-tantivy-a@sha256:d13dee4839c3e56ac1f6dd4f0d14cb4514186221b0f5a6d3037cfb92f090473d'


def http_json(url):
    request = urllib.request.Request(url,headers={'User-Agent':'typeahead-lifecycle-preflight/1'})
    with urllib.request.urlopen(request,timeout=15) as response:
        raw = response.read(1024**2+1)
    if len(raw)>1024**2:raise ValueError('Metadata response exceeds bound')
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description='Prepare the inactive candidate only after a verified full refresh; never promote.')
    parser.add_argument('--refresh-run',required=True,type=UUID)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--submit',action='store_true')
    args = parser.parse_args()
    if os.environ.get('PREFECT_API_URL')!='https://prefect-server.waow.tech/api':
        raise ValueError('Use the production-configured Prefect wrapper')
    saved = None
    if args.output.exists():
        if args.output.is_symlink() or args.output.stat().st_size>65536:
            raise ValueError('Invalid preparation record')
        saved = json.loads(args.output.read_bytes())
        if saved['refresh_run_id']!=str(args.refresh_run) or saved['publisher']!=PUBLISHER:
            raise ValueError('Existing preparation belongs to another release')
        operation = saved.get('parameters',{}).get('tantivy_operation')
        if not isinstance(operation,dict) or operation.get('operation')!='prepare' or operation.get('request_id')!=saved.get('request_id') or operation.get('app')!='typeahead-tantivy-b' or operation.get('image')!=IMAGE:
            raise ValueError('Existing record does not identify this candidate preparation')
    request_id = saved['request_id'] if saved else uuid4().hex
    tag = 'typeahead-lifecycle-request-'+request_id
    def save(record):
        temporary = args.output.with_suffix('.update.tmp')
        with temporary.open('x') as output:
            output.write(json.dumps(record,indent=2)+'\n');output.flush();os.fsync(output.fileno())
        temporary.replace(args.output)
    with get_client(sync_client=True) as client:
        if saved:
            if saved.get('run_id'):
                try:existing = [client.read_flow_run(UUID(saved['run_id']))]
                except ObjectNotFound:
                    print(json.dumps(dict(submitted=False,run_id=saved['run_id'],action='reconcile missing execution')))
                    return 2
            else:
                existing = client.read_flow_runs(flow_run_filter=FlowRunFilter(
                    tags=FlowRunFilterTags(all_=[tag])),limit=2)
            if existing:
                if len(existing)!=1 or existing[0].deployment_id!=DEPLOYMENT or existing[0].parameters.get('tantivy_operation')!=saved['parameters']['tantivy_operation'] or existing[0].parameters.get('automatic_refresh') is not False or existing[0].parameters.get('resume_rollout'):
                    raise ValueError('Existing submission identity is ambiguous')
                run = existing[0]
                execution = (run.job_variables or {}).get('env',{})
                if execution.get('MPS_PIN')!='@'+PUBLISHER or execution.get('INDEX_TANTIVY_SOURCE_REVISION')!=SOURCE or execution.get('INDEX_TANTIVY_ADAPTER_SHA256')!=INSTALLER:
                    raise ValueError('Existing execution uses different release pins')
                print(json.dumps(dict(submitted=False,reconciled=True,run_id=str(run.id),state=run.state_name)))
                return 0 if not run.state.is_final() or run.state.is_completed() else 1
            if saved.get('submission_intent'):
                print(json.dumps(dict(submitted=False,request_id=request_id,action='reconcile unknown submission outcome')))
                return 2
        refresh = client.read_flow_run(args.refresh_run)
        if refresh.deployment_id!=DEPLOYMENT or refresh.parameters.get('automatic_refresh') is not True or refresh.parameters.get('resume_rollout') or refresh.parameters.get('tantivy_operation'):
            raise ValueError('A full automatic refresh is required')
        if refresh.state is None or not refresh.state.is_completed():
            print(json.dumps(dict(submitted=False,precondition='full refresh must complete',
                refresh_run_id=str(refresh.id),state=refresh.state_name)))
            return 2
        if refresh.end_time is None or not 0<=time.time()-refresh.end_time.timestamp()<4*3600:
            raise ValueError('Completed refresh is outside the trial window')
        deployment = client.read_deployment(DEPLOYMENT)
        variables = copy.deepcopy(deployment.job_variables)
        environment = variables['env']
        if environment.get('MPS_PIN')!='@52fe3e7e48c5c91fc83bddeade664813658bcd8c':
            raise ValueError('Registered publisher changed; reconcile before a manual trial')
        def secret(name):return Secret.load(name).get()
        storage = boto3.client('s3',endpoint_url=secret('index-r2-endpoint'),region_name='auto',
            aws_access_key_id=secret('index-r2-access-key-id'),
            aws_secret_access_key=secret('index-r2-secret-access-key'))
        bucket = secret('index-r2-bucket')
        def object_json(key):
            response = storage.get_object(Bucket=bucket,Key=key)
            with response['Body'] as body:raw = body.read(1024**2+1)
            if len(raw)>1024**2:raise ValueError('Stored metadata exceeds bound')
            return json.loads(raw),response['ETag']
        route,etag = object_json('control/search-routing.json')
        current = route['current']
        if route.get('mutation') or current['origin']!='https://typeahead-tantivy-a.fly.dev' or route.get('previous',{}).get('origin')!='https://typeahead-tantivy-b.fly.dev':
            raise ValueError('Fresh serving A and unreserved inactive B are required')
        build = current['build_id']
        if not build.endswith('-cp-'+refresh.id.hex[:10]):
            raise ValueError('Route is not serving the completed full refresh')
        for app in ['typeahead-tantivy-a','typeahead-tantivy-b']:
            try:storage.head_object(Bucket=bucket,Key=f'control/tantivy-slots/{app}/pending.json')
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode']!=404:raise
            else:raise ValueError('Another operation owns a slot')
        health = http_json('https://typeahead.waow.tech/health/freshness')
        if health.get('build_id')!=build or health.get('ok') is not True or health.get('stale') is not False or health.get('probe_ok') is not True or health.get('coverage_deadline',0)-time.time()<18*3600:
            raise ValueError('Fresh public coverage with 18 hours remaining is required')
        artifact,_ = object_json(f'releases/search-tantivy/{build}/manifest.json')
        if artifact['active_build']!=build or not 0<artifact['byte_size']<=8*1024**3:
            raise ValueError('Candidate bundle exceeds the declared trial bound')
        ci = http_json('https://spindle.zzstoatzz.io/xrpc/org.tangled.ci.queryPipelines?repo=did:plc:xrndz6e42qj6ygbclfmruugv&commits='+PUBLISHER+'&limit=1')
        pipeline = ci['pipelines'][0]
        if pipeline['commit']!=PUBLISHER or pipeline['trigger']['ref']!='refs/heads/main' or not any(w['name']=='deploy.yml' and w['status']=='success' for w in pipeline['workflows']):
            raise ValueError('Exact publisher main CI must pass')
        operation = dict(operation='prepare',build_id=build,image=IMAGE,request_id=request_id,
            app='typeahead-tantivy-b',retire_previous_tantivy=True,
            release_receipt_key=environment['INDEX_TANTIVY_RELEASE_RECEIPT_KEY'],
            release_receipt_sha256=environment['INDEX_TANTIVY_RELEASE_RECEIPT_SHA256'])
        record = dict(request_id=request_id,refresh_run_id=str(refresh.id),publisher=PUBLISHER,
            source=SOURCE,installer=INSTALLER,parameters=dict(automatic_refresh=False,
                resume_rollout=False,tantivy_operation=operation),
            observed_route=route,observed_etag=etag,coverage_deadline=health['coverage_deadline'],
            bundle_bytes=artifact['byte_size'],prepared_at=time.time())
        if saved and saved['parameters']!=record['parameters']:
            raise ValueError('Existing preparation parameters changed')
        if not saved:
            with args.output.open('x') as output:output.write(json.dumps(record,indent=2)+'\n')
        if not args.submit:
            print(json.dumps(dict(submitted=False,preflight_passed=True,request_id=request_id,build_id=build)))
            return 0
        environment.update(MPS_PIN='@'+PUBLISHER,INDEX_TANTIVY_SOURCE_REVISION=SOURCE,
            INDEX_TANTIVY_ADAPTER_SHA256=INSTALLER)
        variables['command'] = "env PREFECT_PLUGINS_ENABLED=true PREFECT_PLUGINS_ALLOW=mps-secrets uv run --no-project --with 'my-prefect-server @ git+https://github.com/zzstoatzz/my-prefect-server.git@"+PUBLISHER+"' --with prefect==3.8.2 prefect flow-run execute"
        record['submission_intent'] = True
        save(record)
        try:
            run = client.create_flow_run_from_deployment(DEPLOYMENT,parameters=record['parameters'],
                job_variables=variables,tags=[tag],idempotency_key=tag)
        except Exception as error:
            print(json.dumps(dict(submitted=None,request_id=request_id,error_type=type(error).__name__,
                action='reconcile this request before any new submission')))
            return 2
        record['run_id'] = str(run.id)
        save(record)
        print(json.dumps(dict(submitted=True,run_id=str(run.id),request_id=request_id,build_id=build)))
        return 0


if __name__=='__main__':raise SystemExit(main())

"""Read completed sync progress tied to the sole public Fly machine generation."""
import datetime
import json
import os
import re
import subprocess
import time

APP='typeahead-search-compact'
EXPECTED={'id':'8d4916be311008','instance_id':'01M5CXE7SXEP6Q7S4EJ5ER96M9',
          'digest':'sha256:7df51c4b6ba2b26dcfc1aeb15b7821c7ec0caf6594cb511fc93ddbe946101938'}


def json_stream(text):
    decoder=json.JSONDecoder()
    while text.strip():
        text=text.lstrip();value,end=decoder.raw_decode(text)
        yield value
        text=text[end:]


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def fingerprint(machines):
    configured=[m for m in machines if m.get('config',{}).get('services')]
    if any(type(m.get('cordoned')) is not bool for m in configured):
        raise ValueError('Missing machine routing state')
    public=[m for m in configured if not m['cordoned']]
    if len(public)!=1 or public[0]['state']!='started':
        raise ValueError('Expected one started public search machine')
    m=public[0]
    identity={'id':m['id'],'instance_id':m['instance_id'],'digest':m['image_ref']['digest']}
    if identity!=EXPECTED:
        raise ValueError('Serving machine generation changed')
    return {**identity,'updated_at':m['updated_at']}


def completed_progress(before,after,records,now):
    identity=fingerprint(before)
    if fingerprint(after)!=identity:
        raise ValueError('Serving generation changed during observation')
    completed=[]
    errors=[]
    for record in records:
        if record.get('instance')!=identity['id']:
            continue
        at=timestamp(record['timestamp'])
        if at<timestamp(identity['updated_at']):
            continue
        if at>now:
            raise ValueError('Future machine log timestamp')
        message=record.get('message','')
        if re.fullmatch(r'info\(sync\): incremental: done \(updated=\d+, cleared=\d+, deleted=\d+\)',message):
            completed.append(at)
        if 'incremental: done with errors' in message:
            errors.append(at)
    if not completed or (errors and max(errors)>=max(completed)):
        raise ValueError('No current successful completed sync')
    return {'identity':identity,'observed_at':now,'completed_at':max(completed),
            'source':'fly-machine-completed-sync-log'}


def fetch_progress():
    fly=os.environ.get('PDS_FLY_BIN','fly')
    def command(*args):
        return subprocess.check_output([fly,*args],text=True,timeout=10)
    before=json.loads(command('machine','list','-a',APP,'--json'))
    identity=fingerprint(before)
    records=list(json_stream(command('logs','-a',APP,'--machine',identity['id'],'--no-tail','--json')))
    after=json.loads(command('machine','list','-a',APP,'--json'))
    return completed_progress(before,after,records,time.time())

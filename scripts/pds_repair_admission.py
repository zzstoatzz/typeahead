"""Cloud-host, serving disk and existing live-health gates for repair stages.

The required preflight callback supplies independently checked authority,
writer-exclusion, policy and recovery evidence. No production CLI is enabled.
"""
import os
from pathlib import Path
import subprocess
import time

from pds_fly_progress import EXPECTED
from pds_repair_canary import baseline_budget,health


def parse_available(output):
    rows=[line.split() for line in output.splitlines() if line.strip()]
    if not rows or len(rows[-1])!=2 or rows[-1][1]!='/data':
        raise ValueError('Unexpected serving filesystem response')
    value=rows[-1][0]
    if not value.isascii() or not value.isdigit():raise ValueError('Invalid available-byte count')
    return int(value)


def storage():
    command=[os.environ.get('PDS_FLY_BIN','fly'),'ssh','console','-a','typeahead-search-compact',
             '--machine',EXPECTED['id'],'-C','df -B1 --output=avail,target /data']
    result=subprocess.run(command,capture_output=True,text=True,check=True,timeout=20)
    return {'observed_at':time.time(),'machine':EXPECTED['id'],'available_bytes':parse_available(result.stdout)}


class Admission:
    def __init__(self,baseline,cloud_machine_id,min_free_bytes,preflight):
        if not cloud_machine_id or type(min_free_bytes) is not int or min_free_bytes<1 or not callable(preflight):
            raise ValueError('Explicit cloud, storage and additional preflight gates required')
        self.cloud_machine_id=cloud_machine_id;self.min_free_bytes=min_free_bytes;self.preflight=preflight
        self.budget,self.previous=baseline_budget(baseline)
        if 'search_progress' not in self.previous:
            raise ValueError('Machine-bound completed-sync baseline required')

    def __call__(self):
        if Path('/etc/machine-id').read_text().strip()!=self.cloud_machine_id:
            raise RuntimeError('Wrong executor; refusing home-network bulk repair')
        external=self.preflight()
        if not isinstance(external,dict) or not external:raise ValueError('Additional preflight evidence missing')
        disk=storage()
        if disk['available_bytes']<self.min_free_bytes:raise RuntimeError('Serving disk reserve reached')
        current=health(self.previous,self.budget)
        if not 0<=time.time()-disk['observed_at']<=20:raise RuntimeError('Serving disk evidence is stale')
        self.previous=current
        return {'cloud_machine_id':self.cloud_machine_id,'storage':disk,'health':current,
                'budget':self.budget,'additional_preflight':external}

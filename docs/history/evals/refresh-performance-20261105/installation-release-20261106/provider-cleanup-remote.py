import asyncio,json,time
from dotenv import dotenv_values
from mps.hcloud_builder import HCloud,OWNER,PURPOSE
prefixes=['prefect-build-d91154ed96054c77b6a4633f3aa41c45-','prefect-build-1672f8e7c8b24c0184ba23eb07481529-']
async def main():
 values=dotenv_values('/home/stoat/.config/prefect-home/hcloud-builder.env')
 async with HCloud(values['HCLOUD_TOKEN']) as c:
  out={}
  for resource in ['servers','primary_ips','ssh_keys']:
   page=1;found=[]
   while True:
    v=await c.request('GET',resource,params={'label_selector':f'owner={OWNER},purpose={PURPOSE}','page':page,'per_page':50})
    found.extend({k:s.get(k) for k in ['id','name','status']} for s in v[resource] if any(s.get('name','').startswith(p) for p in prefixes))
    if not v.get('meta',{}).get('pagination',{}).get('next_page'):break
    page+=1
   out[resource]=found
  print(json.dumps(dict(at=time.time(),manual_operation_resources=out,clean=not any(out.values()))))
asyncio.run(main())

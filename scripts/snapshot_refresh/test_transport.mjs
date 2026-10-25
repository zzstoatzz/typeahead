import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const mf = new Miniflare({modules:true,script:readFileSync(process.argv[2], 'utf8'),compatibilityDate:'2024-12-01',r2Buckets:['SEARCH_CONTROL']});
const bucket = await mf.getR2Bucket('SEARCH_CONTROL');
const base='https://typeahead-search-transport.n8-3e9.workers.dev';
const route={version:1,current:{origin:'https://typeahead-startup-20261024.fly.dev',build_id:'b1792896197-ebe5-px05'}};
try {
 assert.equal((await mf.dispatchFetch(base+'/health')).status,503);
 await bucket.put('control/search-routing.json',JSON.stringify(route));
 let r=await mf.dispatchFetch(base+'/health');assert.equal(r.status,200);assert.equal((await r.json()).ready,true);
 r=await mf.dispatchFetch(base+'/search?q=zzstoatzz.io&limit=1');assert.equal(r.status,200);assert.equal((await r.json()).actors[0].handle,'zzstoatzz.io');
 r=await mf.dispatchFetch(base+'/search?q=a&pds=selfhosted.social&limit=5');assert.equal(r.status,200);assert.equal(r.headers.get('X-Typeahead-Pds'),'selfhosted.social');
 assert.equal((await mf.dispatchFetch(base+'/admin/refresh',{method:'POST',headers:{Authorization:'Bearer isolated-invalid-token','Content-Type':'application/json'},body:'{}'})).status,401);
 assert.equal((await mf.dispatchFetch(base+'/unlisted')).status,404);
 assert.equal((await mf.dispatchFetch('https://example.invalid/search?q=a')).status,404);
 for(const bad of ['not json',JSON.stringify({version:1,current:{origin:'https://example.invalid',build_id:route.current.build_id}})]) {
  await bucket.put('control/search-routing.json',bad);await new Promise(resolve=>setTimeout(resolve,1100));assert.equal((await mf.dispatchFetch(base+'/health')).status,503);
 }
 await bucket.put('control/search-routing.json',JSON.stringify({...route,current:{...route.current,build_id:'b1792896198-test'}}));
 assert.equal((await (await mf.dispatchFetch(base+'/route')).json()).build_id,'b1792896198-test');
 console.log('Real workerd + R2: missing/corrupt route fails closed; route update, health, search, scoped acknowledgment, admin auth and origin restrictions pass');
} finally {await mf.dispose()}

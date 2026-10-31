import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

test("workerd search forwards acknowledged scopes, separates edge cache and preserves failures", async () => {
  const directory = await mkdtemp(join(tmpdir(), "typeahead-search-contract-"));
  const entry = join(directory, "worker.ts");
  const owner = new URL("./search.ts", import.meta.url).pathname;
  await writeFile(entry, `import {handleSearch} from ${JSON.stringify(owner)}; export default {fetch(r,env,ctx){return handleSearch(r,env.DB,env,ctx)}}`);
  const built = await Bun.build({ entrypoints: [entry], target: "browser" });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({ workers: [
    { name: "public", modules: true, script: await built.outputs[0].text(), compatibilityDate: "2026-10-29", d1Databases: ["DB"], bindings: { SEARCH_BACKEND_URL: "https://backend.test" }, serviceBindings: { SEARCH_TRANSPORT: "engine" }, outboundService: "bluesky" },
    { name: "engine", modules: true, script: `let calls=0,flaky=0;export default {async fetch(r){calls++;const p=new URL(r.url).searchParams;const q=p.get('q');if(q==='slow')await new Promise(resolve=>setTimeout(resolve,10000));if(q==='flaky'&&flaky++===0)return Response.json({error:'down'},{status:503});if(q==='bad')return Response.json({error:'internal details not public'},{status:400});if(['down','bothdown','badfallback'].includes(q))return Response.json({error:'SearchBudgetExceeded'},{status:503});if(q==='empty')return Response.json({actors:[]});if(q==='identity')return Response.json({actors:[{did:'did:plc:invalid',handle:'handle.invalid'},{did:'did:plc:visible',handle:'visible.test'}]});if(q==='broken')return new Response('badjson');if(q==='missing')return Response.json({error:'not-an-actors-envelope'});if(q==='duplicate')return Response.json({actors:[{did:'did:plc:duplicate',handle:'a.test'},{did:'did:plc:duplicate',handle:'b.test'}]});const h=new Headers({'Content-Type':'application/json'});if(q!=='legacy'){if(p.has('pds'))h.set('X-Typeahead-Pds',p.get('pds'));if(p.has('prefer_pds')){h.set('X-Typeahead-Prefer-Pds',p.get('prefer_pds'));h.set('X-Typeahead-Preference',p.get('preference'));if(p.has('boost')){const b=new DataView(new ArrayBuffer(4));b.setFloat32(0,Number(p.get('boost')));h.set('X-Typeahead-Boost',q==='badack'?'00000000':b.getUint32(0).toString(16).padStart(8,'0'));}}}return new Response(JSON.stringify({actors:[{did:'did:plc:fixture',handle:'fixture.test',displayName:JSON.stringify({q,limit:p.get('limit'),pds:p.get('pds'),preferred:p.get('prefer_pds'),mode:p.get('preference'),boost:p.get('boost'),calls})}]}),{headers:h});}}` },
    { name: "bluesky", modules: true, script: `export default {fetch(r){const p=new URL(r.url).searchParams;const q=p.get('q');if(q==='bothdown')return Response.json({error:'unavailable'},{status:503});if(q==='badfallback')return new Response('not-json');return Response.json({actors:[{did:'did:plc:fallback',handle:'fallback.test',displayName:q}]});}}` },
  ] });
  try {
    const db = await mf.getD1Database("DB");
    await db.exec("CREATE TABLE metrics(hour INTEGER PRIMARY KEY, searches INTEGER DEFAULT 0,total_ms REAL DEFAULT 0,cache_hits INTEGER DEFAULT 0,cache_ms REAL DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER,PRIMARY KEY(bucket,domain));");
    const request = (query: string) => mf.dispatchFetch(`https://public.test/xrpc/tech.waow.typeahead.searchActors?${query}`);
    for (const query of ["q=x&limit=0", "q=x&limit=101", "q=x&limit=10x", "q=x&boost=10", "q=x&prefer_pds=acme.test&preference=oops", "q=x&prefer_pds=acme.test&boost=-1"]) expect((await request(query)).status).toBe(400);
    for (const query of ["q=legacy&pds=acme.test", "q=legacy&prefer_pds=acme.test", "q=badack&prefer_pds=acme.test&boost=100"]) {
      const response = await request(query); expect(response.status).toBe(503); expect(await response.json()).toMatchObject({ error: "PdsScopeUnavailable" });
    }
    for (const q of ["down", "broken", "missing", "duplicate", "slow"]) {
      const response = await request(`q=${q}`); expect(response.status).toBe(200);
      expect(response.headers.get("X-Typeahead-Engine")).toBe("bluesky");
      expect(response.headers.get("X-Typeahead-Failover")).toBe("automatic");
      expect(response.headers.get("Cache-Control")).toBe("no-store");
      expect(response.headers.get("Access-Control-Expose-Headers")).toContain("X-Typeahead-Failover");
      expect(await response.json()).toMatchObject({ actors: [{ handle: "fallback.test" }] });
    }
    for (const q of ["bothdown", "badfallback"]) {
      const response = await request(`q=${q}`); expect(response.status).toBe(503);
      expect(await response.json()).toMatchObject({ error: "SearchUnavailable" });
    }
    const scopedFailure = await request("q=down&pds=acme.test");
    expect(scopedFailure.status).toBe(503);
    expect(scopedFailure.headers.get("X-Typeahead-Failover")).toBeNull();
    const first = await request("q=flaky");
    expect(first.headers.get("X-Typeahead-Failover")).toBe("automatic");
    const second = await request("q=flaky");
    expect(second.headers.get("X-Typeahead-Failover")).toBeNull();
    expect(await second.json()).toMatchObject({ actors: [{ handle: "fixture.test" }] });
    const recovered = await request("q=recovered");
    expect(recovered.status).toBe(200);
    expect(recovered.headers.get("X-Typeahead-Failover")).toBeNull();
    expect(await recovered.json()).toMatchObject({ actors: [{ handle: "fixture.test" }] });
    const empty = await request("q=empty"); expect(empty.status).toBe(200); expect(await empty.json()).toEqual({ actors: [] });
    const identity = await request("q=identity"); expect(identity.status).toBe(200); expect(await identity.json()).toEqual({ actors: [{ did: "did:plc:visible", handle: "visible.test" }] });
    const bad = await request("q=bad"); expect(bad.status).toBe(400); expect(await bad.text()).not.toContain("internal details");
    const queries = ["q=cache", "q=cache&pds=acme.test", "q=cache&prefer_pds=acme.test", "q=cache&prefer_pds=acme.test&boost=100", "q=cache&prefer_pds=acme.test&boost=200", "q=cache&limit=25"];
    const fixtureEnvelope = z.object({ actors: z.array(z.object({ did: z.string(), handle: z.string(), displayName: z.string() })) });
    const answers = [];
    for (const query of queries) {
      const response = await request(query); expect(response.status).toBe(200);
      const answer = fixtureEnvelope.parse(await response.json()); answers.push(answer);
    }
    expect(new Set(answers.map(answer => answer.actors[0].displayName)).size).toBe(queries.length);
    const boost = JSON.parse(answers[3].actors[0].displayName);
    expect(boost).toMatchObject({ preferred: "acme.test", mode: "boost", boost: "100", limit: "10" });
    let cached;
    for (let attempt = 0; attempt < 10; attempt++) {
      const response = await request("q=cache&prefer_pds=HTTP%3A%2F%2FACME.TEST.%2F&preference=boost&boost=1e2");
      cached = await response.json();
      if (response.headers.get("Server-Timing")?.includes('desc="hit"')) break;
      await Bun.sleep(10);
    }
    expect(cached).toEqual(answers[3]);
    const alias = await mf.dispatchFetch("https://public.test/xrpc/app.bsky.actor.searchActorsTypeahead?q=cache&prefer_pds=acme.test&boost=100");
    expect(await alias.json()).toEqual(answers[3]);
  } finally { await mf.dispose(); await rm(directory, { recursive: true, force: true }); }
}, 30000);

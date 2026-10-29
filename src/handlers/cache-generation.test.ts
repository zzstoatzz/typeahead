import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

test("workerd cache generations isolate promotion and rollback without backend work on hits", async () => {
  const directory = await mkdtemp(join(tmpdir(), "typeahead-cache-generation-"));
  const entry = join(directory, "worker.ts");
  await writeFile(entry, `import {handleSearch} from ${JSON.stringify(new URL("./search.ts", import.meta.url).pathname)};export default{fetch(r,e,c){if(new URL(r.url).pathname==='/__fixture/control')return e.SEARCH_TRANSPORT.fetch(r);return handleSearch(r,e.DB,{...e,SEARCH_CACHE_GENERATION:r.headers.get('X-Fixture-Generation')??undefined},c)}}`);
  const built = await Bun.build({ entrypoints: [entry], target: "browser" });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({ workers: [
    { name: "public", modules: true, script: await built.outputs[0].text(), compatibilityDate: "2026-10-29", d1Databases: ["DB"], bindings: { SEARCH_BACKEND_URL: "https://fixture-engine.test" }, serviceBindings: { SEARCH_TRANSPORT: "engine" } },
    { name: "engine", modules: true, script: `let engine='native',calls=0;export default{fetch(r){const u=new URL(r.url);if(u.pathname==='/__fixture/control'){if(u.searchParams.has('engine'))engine=u.searchParams.get('engine');return Response.json({engine,calls})}calls++;const headers=new Headers({'Content-Type':'application/json'});if(u.searchParams.get('q')!=='unidentified'){headers.set('X-Typeahead-Engine',engine);headers.set('X-Typeahead-Format',engine==='native'?'FLATPX05':'tantivy-0.27-f32-did-policy-v3');headers.set('X-Typeahead-Build',engine==='native'?'b1792908655-native':'b1792908655-tantivy')}return Response.json({actors:[{did:'did:plc:fixture',handle:engine+'.test'}]},{headers})}}` },
  ] });
  const request = (generation?: string, q = "same") => mf.dispatchFetch(`https://public.test/search?q=${q}`, { headers: generation === undefined ? {} : { "X-Fixture-Generation": generation } });
  const control = async (engine?: string) => {
    const response = await mf.dispatchFetch(`https://public.test/__fixture/control${engine ? `?engine=${engine}` : ""}`);
    return z.object({ engine: z.string(), calls: z.number() }).parse(await response.json());
  };
  async function hit(generation: string) {
    for (let attempt = 0; attempt < 10; attempt++) {
      const response = await request(generation);
      if (response.headers.get("Server-Timing")?.includes('desc="hit"')) return response;
      await response.arrayBuffer(); await Bun.sleep(10);
    }
    throw new Error("Cache did not settle");
  }
  try {
    const db = await mf.getD1Database("DB");
    await db.exec("CREATE TABLE metrics(hour INTEGER PRIMARY KEY, searches INTEGER DEFAULT 0,total_ms REAL DEFAULT 0,cache_hits INTEGER DEFAULT 0,cache_ms REAL DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER,PRIMARY KEY(bucket,domain));");
    const native = await request("A"); expect(native.status).toBe(200); expect(native.headers.get("X-Typeahead-Engine")).toBe("native"); expect(native.headers.get("X-Typeahead-Cache-Generation")).toBe("A"); await native.arrayBuffer();
    await (await hit("A")).arrayBuffer();
    const before = await control();
    await control("tantivy");
    const old = await hit("A"); expect(old.headers.get("X-Typeahead-Engine")).toBe("native"); expect(await old.json()).toMatchObject({ actors: [{ handle: "native.test" }] }); expect((await control())).toEqual({ ...before, engine: "tantivy" });
    const promoted = await request("B"); expect(promoted.headers.get("X-Typeahead-Engine")).toBe("tantivy"); expect(promoted.headers.get("X-Typeahead-Format")).toBe("tantivy-0.27-f32-did-policy-v3"); expect(promoted.headers.get("X-Typeahead-Build")).toBe("b1792908655-tantivy"); expect(promoted.headers.get("Server-Timing")).toContain('desc="miss"'); await promoted.arrayBuffer();
    await (await hit("B")).arrayBuffer();
    await control("native");
    const retained = await hit("B"); expect(retained.headers.get("X-Typeahead-Engine")).toBe("tantivy"); await retained.arrayBuffer();
    const rollback = await request("C"); expect(rollback.headers.get("X-Typeahead-Engine")).toBe("native"); expect(rollback.headers.get("Server-Timing")).toContain('desc="miss"'); await rollback.arrayBuffer();
    const beforeHit = await control(); await (await hit("C")).arrayBuffer(); expect(await control()).toEqual(beforeHit);
    const unknown = await request("D", "unidentified"); expect(unknown.headers.get("X-Typeahead-Engine")).toBeNull(); await unknown.arrayBuffer();
    const legacy = await request(); expect(legacy.headers.get("X-Typeahead-Cache-Generation")).toBe("legacy"); await legacy.arrayBuffer();
    const invalid = await request("invalid/generation"); expect(invalid.status).toBe(503); expect(await invalid.json()).toMatchObject({ error: "SearchConfigurationUnavailable" });
  } finally { await mf.dispose(); await rm(directory, { recursive: true, force: true }); }
}, 30000);

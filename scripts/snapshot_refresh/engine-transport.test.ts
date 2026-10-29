import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

test("engine route conditional promotion and rollback use isolated filesystem-backed R2", async () => {
  const directory = await mkdtemp(join(tmpdir(), "typeahead-engine-route-"));
  const entry = join(directory, "worker.ts");
  await writeFile(entry, `import transport from ${JSON.stringify(resolve("src/search-transport.ts"))}; import {conditionalRouteUpdate,ROUTE_KEY} from ${JSON.stringify(resolve("src/search-route.ts"))};export default {async fetch(r,e,c){if(new URL(r.url).pathname==='/__control'){if(r.method==='POST'){const ok=await conditionalRouteUpdate(e.SEARCH_CONTROL,r.headers.get('If-Match'),await r.json());return Response.json({ok},{status:ok?200:409})}const x=await e.SEARCH_CONTROL.get(ROUTE_KEY);return x?new Response(x.body,{headers:{ETag:x.etag}}):new Response(null,{status:404})}const u=new URL(r.url);u.protocol="https:";u.host="typeahead-search-transport.n8-3e9.workers.dev";u.port="";return transport.fetch(new Request(u,r),e,c)}}`);
  const built = await Bun.build({ entrypoints: [entry], target: "browser" });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({ r2Persist: join(directory, "r2"), workers: [
    { name: "transport", script: await built.outputs[0].text(), modules: true, compatibilityDate: "2026-10-29", r2Buckets: ["SEARCH_CONTROL"], outboundService: "origin" },
    { name: "origin", modules: true, script: `export default {fetch(r){const u=new URL(r.url);if(r.method==='POST')return Response.json({error:'native-auth-fixture'},{status:401});const h=new Headers({'Content-Type':'application/json'});if(u.hostname==='typeahead-tantivy-dev.fly.dev'&&u.searchParams.get('q')!=='wrong'){h.set('X-Typeahead-Engine','tantivy');h.set('X-Typeahead-Build','b1792908655-test');h.set('X-Typeahead-Format','tantivy-0.27-f32-did-expiry-v4');h.set('X-Typeahead-Binary-Sha256',(u.searchParams.get('q')==='wrongbinary'?'d':'c').repeat(64));h.set('X-Typeahead-Retrieval-Strategy',u.searchParams.get('q')==='wrongstrategy'?'global-merge':'per-segment')}return new Response(JSON.stringify({origin:u.origin,actors:[]}),{headers:h})}}` },
  ] });
  const base = "https://typeahead-search-transport.n8-3e9.workers.dev";
  const native = { origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1792908655-test", source_watermark: 1792908650, machine_id: "8e3d79b70dd748", volume_id: "vol_fixture", legacy_build_id: "b1792908655-legacy" };
  const candidate = { engine: "tantivy", origin: "https://typeahead-tantivy-dev.fly.dev", build_id: native.build_id, format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "per-segment" };
  const control = async () => {
    const response = await mf.dispatchFetch(`${base}/__control`);
    return { etag: response.headers.get("ETag"), value: await response.json() };
  };
  const update = (value: object, etag: string | null) => mf.dispatchFetch(`${base}/__control`, { method: "POST", headers: etag ? { "If-Match": etag } : {}, body: JSON.stringify(value) });
  try {
    expect((await update(native, null)).status).toBe(200);
    const before = await control();
    expect((await update(candidate, before.etag)).status).toBe(200);
    const promoted = await control();
    expect(promoted.value).toEqual({ version: 1, current: candidate, previous: native, native_rollback: native });
    expect((await update(native, before.etag)).status).toBe(409);
    expect((await control()).value).toEqual(promoted.value);
    expect((await mf.dispatchFetch(`${base}/route`)).status).toBe(200);
    const search = await mf.dispatchFetch(`${base}/search?q=a`); expect(search.status).toBe(200); expect(await search.json()).toMatchObject({ origin: candidate.origin });
    const mismatch = await mf.dispatchFetch(`${base}/search?q=wrong`); expect(mismatch.status).toBe(503); expect(await mismatch.json()).toEqual({ error: "SearchBackendIdentityMismatch" });
    for(const q of ["wrongbinary","wrongstrategy"]) {const bad=await mf.dispatchFetch(`${base}/search?q=${q}`);expect(bad.status).toBe(503);expect(await bad.json()).toEqual({error:"SearchBackendIdentityMismatch"});}
    const refresh = await mf.dispatchFetch(`${base}/admin/refresh`, { method: "POST", body: "{}" }); expect(refresh.status).toBe(503); expect(await refresh.json()).toMatchObject({ error: "NativeRefreshUnsupported" });
    expect((await mf.dispatchFetch(`${base}/debug/index`)).status).toBe(503);
    const refreshedCandidate = { ...candidate, admission_sha256: "b".repeat(64) };
    expect((await update(refreshedCandidate, promoted.etag)).status).toBe(200);
    const refreshed = await control();
    expect(refreshed.value).toEqual({ version: 1, current: refreshedCandidate, previous: candidate, native_rollback: native });
    expect((await update(native, promoted.etag)).status).toBe(409);
    expect((await control()).value).toEqual(refreshed.value);
    expect((await update(native, refreshed.etag)).status).toBe(200);
    expect((await control()).value).toEqual({ version: 1, current: native, previous: refreshedCandidate, native_rollback: native });
    await mf.dispatchFetch(`${base}/route`);
    expect((await mf.dispatchFetch(`${base}/admin/refresh`, { method: "POST", body: "{}" })).status).toBe(401);
    const rolledBack = await mf.dispatchFetch(`${base}/search?q=a`); expect(rolledBack.status).toBe(200); expect(rolledBack.headers.get("X-Typeahead-Engine")).toBe("native"); expect(await rolledBack.json()).toMatchObject({ origin: native.origin });
  } finally { await mf.dispose(); await rm(directory, { recursive: true, force: true }); }
}, 30000);

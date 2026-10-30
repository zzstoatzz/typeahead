import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { execFileSync } from "node:child_process";

function buildPreview(): string {
  return execFileSync("bun", ["build", new URL("./worker.ts", import.meta.url).pathname, "--target", "browser"], { encoding: "utf8", timeout: 10000 });
}

test("actual workerd preview is read-only and preserves HTML and dev search semantics", async () => {
  const script = buildPreview();
  const mf = new Miniflare({
    workers: [
      { name: "preview", modules: true, script: script, compatibilityDate: "2026-10-29", bindings: { DEV_SEARCH_BACKEND: "https://typeahead-tantivy-dev.fly.dev" }, serviceBindings: { PRODUCTION_UI: "ui" }, outboundService: "backend" },
      { name: "ui", modules: true, script: `export default {fetch(r) {if (r.method !== 'GET' && r.method !== 'HEAD') return new Response('WRITE REACHED PRODUCTION', {status:599}); return new Response('<!doctype html><title>Production UI fixture</title>', {headers:{'Content-Type':'text/html'}})}}` },
      { name: "backend", modules: true, script: `export default {fetch(r) {const u=new URL(r.url); if(u.searchParams.get('q')==='error') return Response.json({error:'SearchBudgetExceeded'},{status:503}); const headers=new Headers();if(u.searchParams.get('q')!=='missingack'){if(u.searchParams.has('pds'))headers.set('X-Typeahead-Pds',u.searchParams.get('pds'));if(u.searchParams.has('prefer_pds')){headers.set('X-Typeahead-Prefer-Pds',u.searchParams.get('prefer_pds'));headers.set('X-Typeahead-Preference',u.searchParams.get('preference'));headers.set('X-Typeahead-Boost',u.searchParams.get('q')==='wrongboost'?'00000000':'42c80000')}}return Response.json({url:r.url,method:r.method,authorization:r.headers.get('Authorization')},{headers});}}` },
    ],
  });
  try {
    const html = await mf.dispatchFetch("https://preview.test/");
    expect(await html.text()).toBe("<!doctype html><title>Production UI fixture</title>");
    for (const path of ["/request-indexing?handle=example.test", "/admin/ingest", "/search", "/other"]) {
      for (const method of ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
        const response = await mf.dispatchFetch(`https://preview.test${path}`, { method });
        expect(response.status).toBe(503);
        expect(await response.json()).toMatchObject({ error: "DevReadOnly" });
      }
    }
    expect((await mf.dispatchFetch("https://preview.test/admin/ingest")).status).toBe(404);
    for (const path of ["/search", "/xrpc/tech.waow.typeahead.searchActors", "/xrpc/app.bsky.actor.searchActorsTypeahead"]) {
      const response = await mf.dispatchFetch(`https://preview.test${path}?q=alice&prefer_pds=acme.test&preference=boost&boost=100`, { headers: { Authorization: "Bearer must-not-forward" } });
      expect(response.headers.get("Cache-Control")).toBe("no-store");
      expect(await response.json()).toEqual({ url: `https://typeahead-tantivy-dev.fly.dev${path}?q=alice&prefer_pds=acme.test&preference=boost&boost=100`, method: "GET", authorization: null });
    }
    for (const path of ["/search", "/xrpc/tech.waow.typeahead.searchActors", "/xrpc/app.bsky.actor.searchActorsTypeahead"]) {
      for (const query of ["q=missingack&pds=acme.test", "q=missingack&prefer_pds=acme.test", "q=wrongboost&prefer_pds=acme.test&preference=boost&boost=100"]) {
        const response = await mf.dispatchFetch(`https://preview.test${path}?${query}`);
        expect(response.status).toBe(503);
        expect(await response.json()).toMatchObject({ error: "PdsScopeUnavailable" });
        expect(response.headers.get("Cache-Control")).toBe("no-store");
      }
      const malformed = await mf.dispatchFetch(`https://preview.test${path}?q=alice&boost=100`);
      expect(malformed.status).toBe(400);
      expect(await malformed.json()).toMatchObject({ error: "InvalidRequest" });
    }
    const error = await mf.dispatchFetch("https://preview.test/search?q=error");
    expect(error.status).toBe(503);
    expect(await error.json()).toEqual({ error: "SearchBudgetExceeded" });
  } finally { await mf.dispose(); }
}, 30000);

test("the same production HTML uses either managed slot and rejects other backends", async () => {
  const script = buildPreview();
  const mf = new Miniflare({ workers: [
    ...["a", "b", "invalid"].map(slot => ({
      name: slot, modules: true, script, compatibilityDate: "2026-10-29",
      bindings: { DEV_SEARCH_BACKEND: slot === "invalid" ? "https://typeahead.waow.tech" : `https://typeahead-tantivy-${slot}.fly.dev` },
      serviceBindings: { PRODUCTION_UI: "ui" }, outboundService: "backend",
    })),
    { name: "ui", modules: true, script: `export default {fetch(){return new Response('<!doctype html><title>Same production UI</title>')}}` },
    { name: "backend", modules: true, script: `export default {fetch(r){return Response.json({url:r.url},{headers:{'X-Typeahead-Pds':new URL(r.url).searchParams.get('pds')??''}})}}` },
  ] });
  try {
    for (const slot of ["a", "b"]) {
      const worker = await mf.getWorker(slot);
      const response = await worker.fetch("https://preview.test/search?q=alice&pds=acme.test");
      expect(await response.json()).toEqual({ url: `https://typeahead-tantivy-${slot}.fly.dev/search?q=alice&pds=acme.test` });
      expect(response.headers.get("X-Typeahead-Search-Backend")).toBe(`https://typeahead-tantivy-${slot}.fly.dev`);
      expect(await (await worker.fetch("https://preview.test/")).text()).toBe("<!doctype html><title>Same production UI</title>");
    }
    const invalid = await mf.getWorker("invalid");
    const response = await invalid.fetch("https://preview.test/search?q=alice");
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "DevBackendConfigurationInvalid" });
  } finally { await mf.dispose(); }
}, 30000);

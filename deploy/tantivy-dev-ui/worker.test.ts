import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";

test("actual workerd preview is read-only and preserves HTML and dev search semantics", async () => {
  const built = await Bun.build({ entrypoints: [new URL("./worker.ts", import.meta.url).pathname], target: "browser" });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({
    workers: [
      { name: "preview", modules: true, script: await built.outputs[0].text(), compatibilityDate: "2026-10-29", serviceBindings: { PRODUCTION_UI: "ui" }, outboundService: "backend" },
      { name: "ui", modules: true, script: `export default {fetch(r) {if (r.method !== 'GET' && r.method !== 'HEAD') return new Response('WRITE REACHED PRODUCTION', {status:599}); return new Response('<!doctype html><title>Production UI fixture</title>', {headers:{'Content-Type':'text/html'}})}}` },
      { name: "backend", modules: true, script: `export default {fetch(r) {const u=new URL(r.url); if(u.searchParams.get('q')==='error') return Response.json({error:'SearchBudgetExceeded'},{status:503}); return Response.json({url:r.url,method:r.method,authorization:r.headers.get('Authorization')});}}` },
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
    const error = await mf.dispatchFetch("https://preview.test/search?q=error");
    expect(error.status).toBe(503);
    expect(await error.json()).toEqual({ error: "SearchBudgetExceeded" });
  } finally { await mf.dispose(); }
}, 30000);

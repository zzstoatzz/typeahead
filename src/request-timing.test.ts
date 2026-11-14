import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RequestTiming } from "./request-timing";

test("request timing includes awaited limiter rejection and preserves response headers", async () => {
  const server = Bun.serve({ port: 0, async fetch() {
    await Bun.sleep(30);
    return new Response(null, { status: 503 });
  } });
  try {
    const timing = new RequestTiming();
    await expect(timing.limit({ async limit() {
      const response = await fetch(server.url);
      if (!response.ok) throw new Error("limiter unavailable");
      return { success: true };
    } }, "test")).rejects.toThrow("limiter unavailable");
    expect(timing.rateLimitMs).toBeGreaterThanOrEqual(20);
    const response = timing.response(new Response("retained", { status: 503,
      headers: { "Server-Timing": "search;dur=7", "X-Typeahead-Engine": "tantivy" } }));
    expect(response.status).toBe(503);
    expect(await response.text()).toBe("retained");
    expect(response.headers.get("X-Typeahead-Engine")).toBe("tantivy");
    expect(response.headers.get("Server-Timing")).toContain("search;dur=7");
    expect(response.headers.get("Server-Timing")).toContain(`rate_limit;dur=${timing.rateLimitMs}`);
    expect(timing.elapsedMs()).toBeGreaterThanOrEqual(timing.rateLimitMs ?? Infinity);
  } finally { server.stop(true); }
});

test("full Worker times cache hits, validation failures, rate denial and preflight", async () => {
  const server = Bun.serve({ port: 0, async fetch(request) {
    if (new URL(request.url).pathname !== "/search") return new Response(null, { status: 503 });
    await Bun.sleep(30);
    return Response.json({ actors: [{ did: "did:plc:fixture", handle: "fixture.test" }] },
      { headers: { "X-Typeahead-Engine": "tantivy", "X-Typeahead-Build": "fixture" } });
  } });
  const directory = await mkdtemp(join(tmpdir(), "typeahead-request-timing-"));
  const entry = join(directory, "worker.ts");
  await writeFile(entry, `export {default} from ${JSON.stringify(new URL("./index.ts", import.meta.url).pathname)}`);
  const built = await Bun.build({ entrypoints: [entry],
    target: "browser", external: ["node:*", "cloudflare:workers"] });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({ modules: true, script: await built.outputs[0].text(),
    compatibilityDate: "2024-12-01", compatibilityFlags: ["nodejs_compat"],
    bindings: { SEARCH_BACKEND_URL: server.url.toString().replace(/\/$/, "") },
    ratelimits: { RATE_LIMITER: { simple: { limit: 3, period: 60 } } } });
  try {
    const request = (suffix = "?q=fixture&limit=5") => mf.dispatchFetch(
      `https://public.test/xrpc/tech.waow.typeahead.searchActors${suffix}`,
      { headers: { "CF-Connecting-IP": "192.0.2.10" } });
    const first = await request();
    expect(first.status).toBe(200);
    const body = await first.text();
    expect(first.headers.get("Server-Timing")).toContain('desc="miss"');
    await Bun.sleep(30);
    const cached = await request();
    expect(await cached.text()).toBe(body);
    const timing = cached.headers.get("Server-Timing") ?? "";
    expect(timing).toContain('desc="hit"');
    expect(timing.match(/worker;dur=/g)).toHaveLength(1);
    expect(timing.match(/rate_limit;dur=/g)).toHaveLength(1);
    const invalid = await request("?q=fixture&limit=0");
    expect(invalid.status).toBe(400);
    expect(invalid.headers.get("Server-Timing")).toContain("rate_limit;dur=");
    const denied = await request();
    expect(denied.status).toBe(429);
    expect(await denied.json()).toEqual({ error: "rate limited" });
    expect(denied.headers.get("Server-Timing")).toContain("worker;dur=");
    expect(denied.headers.get("Server-Timing")).toContain("rate_limit;dur=");
    const preflight = await mf.dispatchFetch("https://public.test/", { method: "OPTIONS" });
    expect(preflight.status).toBe(204);
    expect(preflight.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(preflight.headers.get("Server-Timing")).toContain("worker;dur=");
    expect(preflight.headers.get("Server-Timing")).not.toContain("rate_limit;");
  } finally { await mf.dispose(); server.stop(true); await rm(directory, { recursive: true, force: true }); }
}, 30000);

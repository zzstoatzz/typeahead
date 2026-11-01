import { expect } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { z } from "zod";
import { liveConfiguration, liveOriginAdapter, nativeIdentity } from "./rehearsal-live-origin";

export const aliases = ["/xrpc/tech.waow.typeahead.searchActors", "/xrpc/app.bsky.actor.searchActorsTypeahead"];
export const transportOrigin = "https://typeahead-search-transport.n8-3e9.workers.dev";
export const route = z.object({ version: z.literal(1), current: z.object({ origin: z.string(), build_id: z.string() }).passthrough(), previous: z.unknown(), native_rollback: z.unknown().optional() });
export const actors = z.object({ actors: z.array(z.object({ did: z.string(), handle: z.string() })) });

const sourcePaths = ["src/handlers/search.ts", "src/handlers/search-contract.ts", "src/search-transport.ts", "src/search-route.ts", "src/validated-search.ts", "scripts/snapshot_refresh/route-rehearsal.ts", "scripts/snapshot_refresh/route-rehearsal.test.ts", "scripts/snapshot_refresh/route-rehearsal-live.ts", "scripts/snapshot_refresh/rehearsal-live-origin.ts"];
export const fingerprints = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash("sha256").update(await readFile(path)).digest("hex")] as const)));

export async function rehearsalScripts(directory: string) {
  const publicEntry = join(directory, "public.ts");
  const transportEntry = join(directory, "transport.ts");
  await writeFile(publicEntry, `import {handleSearch} from ${JSON.stringify(resolve("src/handlers/search.ts"))};
let generation='A';export default{async fetch(r,e,c){const u=new URL(r.url);
if(u.pathname==='/__fixture/generation'){generation=u.searchParams.get('value');return Response.json({generation})}
if(u.pathname==='/__fixture/route')return e.SEARCH_TRANSPORT.fetch(new Request(${JSON.stringify(transportOrigin)}+'/__fixture/route',r));
if(u.pathname==='/__fixture/origin')return e.ORIGIN.fetch(new Request('https://fixture-origin.test/__fixture/control'+u.search,r));
if(!${JSON.stringify(aliases)}.includes(u.pathname))return new Response('fixture path unavailable',{status:404});
return handleSearch(r,e.DB,{...e,SEARCH_CACHE_GENERATION:generation},c)}}`);
  await writeFile(transportEntry, `import transport from ${JSON.stringify(resolve("src/search-transport.ts"))};
import {conditionalRouteUpdate,ROUTE_KEY} from ${JSON.stringify(resolve("src/search-route.ts"))};
export default{async fetch(r,e,c){if(new URL(r.url).pathname==='/__fixture/route'){
if(r.method==='POST'){const ok=await conditionalRouteUpdate(e.SEARCH_CONTROL,r.headers.get('If-Match'),await r.json());return Response.json({ok},{status:ok?200:409})}
const value=await e.SEARCH_CONTROL.get(ROUTE_KEY);return value?new Response(value.body,{headers:{ETag:value.etag}}):new Response(null,{status:404})}
return transport.fetch(r,e,c)}}`);
  async function bundle(entry: string) {
    const output = entry + ".mjs";
    execFileSync("bun", ["build", entry, "--target", "browser", "--outfile", output], { stdio: "pipe", timeout: 10000 });
    return readFile(output, "utf8");
  }
  return { publicScript: await bundle(publicEntry), transportScript: await bundle(transportEntry) };
}

export async function runLiveRehearsal(configuration: ReturnType<typeof liveConfiguration>, adapter: ReturnType<typeof liveOriginAdapter>, evidencePath?: string, configurationSha256?: string) {
  const sourceHashes = await fingerprints();
  const directory = await mkdtemp(join(tmpdir(), "typeahead-live-route-"));
  const scripts = await rehearsalScripts(directory);
  const outbound = (request: Request) => adapter.request(new Request(request.url, { method: request.method }));
  const mf = new Miniflare({ r2Persist: join(directory, "r2"), cachePersist: join(directory, "cache"), workers: [
    { name: "public", modules: true, script: scripts.publicScript, compatibilityDate: "2026-10-29", d1Databases: ["DB"], bindings: { SEARCH_BACKEND_URL: transportOrigin }, serviceBindings: { SEARCH_TRANSPORT: "transport" }, outboundService: outbound },
    { name: "transport", modules: true, script: scripts.transportScript, compatibilityDate: "2026-10-29", r2Buckets: ["SEARCH_CONTROL"], outboundService: outbound },
  ] });
  const publicOrigin = "https://public-rehearsal.test";
  const dispatch = (path: string, init?: Parameters<Miniflare["dispatchFetch"]>[1]) => mf.dispatchFetch(publicOrigin + path, init);
  const control = async () => { const response = await dispatch("/__fixture/route"); return { etag: response.headers.get("ETag"), value: route.parse(await response.json()) }; };
  const update = async (candidate: typeof configuration.native | typeof configuration.candidate, etag: string | null) => {
    const response = await dispatch("/__fixture/route", { method: "POST", headers: etag ? { "If-Match": etag } : {}, body: JSON.stringify(candidate) });
    await response.arrayBuffer(); return response.status;
  };
  const samples: { engine: string; expected_build: string; build_header: string | null; body_sha256: string; actors: number }[] = [];
  const nativeChecks: { phase: string; boundary: string; identity: Awaited<ReturnType<typeof nativeIdentity>> }[] = [];
  let passed = false;
  try {
    const db = await mf.getD1Database("DB", "public");
    await db.exec("CREATE TABLE metrics(hour INTEGER PRIMARY KEY, searches INTEGER DEFAULT 0,total_ms REAL DEFAULT 0,cache_hits INTEGER DEFAULT 0,cache_ms REAL DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER,PRIMARY KEY(bucket,domain));");
    const phase = async (expected: typeof configuration.native | typeof configuration.candidate, generation: string) => {
      const before = expected.engine === "tantivy" ? null : await nativeIdentity(configuration, adapter);
      if (before) nativeChecks.push({ phase: generation, boundary: "before", identity: before });
      await Bun.sleep(1100);
      const transport = await mf.getWorker("transport");
      await (await transport.fetch(transportOrigin + "/route")).arrayBuffer();
      await (await dispatch("/__fixture/generation?value=" + generation)).arrayBuffer();
      const bodies = [];
      for (const alias of aliases) {
        const response = await dispatch(alias + "?q=zzstoatzz.io&limit=10");
        const body = await response.text();
        expect(response.status).toBe(200);
        expect(response.headers.get("X-Typeahead-Engine")).toBe(expected.engine ?? "native");
        const buildHeader = response.headers.get("X-Typeahead-Build");
        if (expected.engine === "tantivy" || buildHeader !== null) expect(buildHeader).toBe(expected.build_id);
        const result = actors.parse(JSON.parse(body));
        expect(result.actors[0]?.handle.toLowerCase()).toBe("zzstoatzz.io");
        if (expected.engine === "tantivy") {
          expect(response.headers.get("X-Typeahead-Binary-Sha256")).toBe(expected.binary_sha256);
          expect(response.headers.get("X-Typeahead-Retrieval-Strategy")).toBe(expected.retrieval_strategy);
        }
        bodies.push(body);
        samples.push({ engine: expected.engine ?? "native", expected_build: expected.build_id, build_header: buildHeader, body_sha256: createHash("sha256").update(body).digest("hex"), actors: result.actors.length });
      }
      expect(bodies[0]).toBe(bodies[1]);
      if (before) {
        const after = await nativeIdentity(configuration, adapter);
        expect(after.build_id).toBe(before.build_id);
        expect(after.source_watermark).toBe(before.source_watermark);
        nativeChecks.push({ phase: generation, boundary: "after", identity: after });
      }
    };
    expect(await update(configuration.native, null)).toBe(200);
    const initial = await control();
    await phase(configuration.native, "live-native-before");
    expect(await update(configuration.candidate, initial.etag)).toBe(200);
    const promoted = await control();
    expect(promoted.value.native_rollback).toEqual(configuration.native);
    await phase(configuration.candidate, "live-tantivy");
    expect(await update(configuration.native, initial.etag)).toBe(409);
    expect((await control()).value).toEqual(promoted.value);
    expect(await update(configuration.native, promoted.etag)).toBe(200);
    const restored = await control();
    expect(restored.value.current).toEqual(configuration.native);
    expect(restored.value.native_rollback).toEqual(configuration.native);
    await phase(configuration.native, "live-native-after");
    expect(await fingerprints()).toEqual(sourceHashes);
    passed = true;
  } finally {
    await mf.dispose(); await rm(directory, { recursive: true, force: true });
    if (evidencePath) await writeFile(evidencePath, JSON.stringify({ schema_version: 1, observed_at: new Date().toISOString(), passed, scope: "local-Worker-and-R2-with-approved-live-GET-backends", source_sha256: sourceHashes, configuration_sha256: configurationSha256, configuration, limits_and_requests: adapter.evidence(), samples, native_identity_checks: nativeChecks, native_identity_method: "read-only health/index probes bracketing native search; absent search build headers retained as null", remote_route_writes: 0, remote_non_GET_requests: 0, remote_corpus_copies: 0, route_settle_wait_ms: 1100, source_freshness_qualified: false, release_qualified: false, production_route_changed: false }, null, 2) + "\n", { flag: "wx" });
  }
}


import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { z } from "zod";

const aliases = ["/xrpc/tech.waow.typeahead.searchActors", "/xrpc/app.bsky.actor.searchActorsTypeahead"];
const transportOrigin = "https://typeahead-search-transport.n8-3e9.workers.dev";
const native = { origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1792908655-native", source_watermark: 1792908650, machine_id: "8e3d79b70dd748", volume_id: "vol_fixture", legacy_build_id: "b1792908655-legacy" };
const tantivy = { engine: "tantivy", origin: "https://typeahead-tantivy-a.fly.dev", build_id: "b1792908655-tantivy", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "global-merge" };
const operator = { ...native, origin: "https://typeahead-search-refresh.fly.dev", build_id: "b1792908655-operator" };
const route = z.object({ version: z.literal(1), current: z.object({ origin: z.string(), build_id: z.string() }).passthrough(), previous: z.unknown(), native_rollback: z.unknown().optional() });
const stats = z.object({ unexpected: z.number(), calls: z.array(z.object({ origin: z.string(), q: z.string(), pds: z.string().nullable(), preferred: z.string().nullable(), preference: z.string().nullable(), boost: z.string().nullable(), ack: z.string() })) });
const actors = z.object({ actors: z.array(z.object({ did: z.string(), handle: z.string() })) });

test("local public handler, actual transport and R2 CAS rehearse promotion and compensation", async () => {
  const started = performance.now();
  const sourcePaths = ["src/handlers/search.ts", "src/handlers/search-contract.ts", "src/search-transport.ts", "src/search-route.ts", "src/validated-search.ts", "scripts/snapshot_refresh/route-rehearsal.test.ts"];
  const fingerprints = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash("sha256").update(await readFile(path)).digest("hex")] as const)));
  const sourceHashes = await fingerprints();
  const directory = await mkdtemp(join(tmpdir(), "typeahead-route-rehearsal-"));
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
  const mf = new Miniflare({ r2Persist: join(directory, "r2"), cachePersist: join(directory, "cache"), workers: [
    { name: "public", modules: true, script: await bundle(publicEntry), compatibilityDate: "2026-10-29", d1Databases: ["DB"], bindings: { SEARCH_BACKEND_URL: transportOrigin }, serviceBindings: { SEARCH_TRANSPORT: "transport", ORIGIN: "origin" }, outboundService: "origin" },
    { name: "transport", modules: true, script: await bundle(transportEntry), compatibilityDate: "2026-10-29", r2Buckets: ["SEARCH_CONTROL"], outboundService: "origin" },
    { name: "origin", modules: true, compatibilityDate: "2026-10-29", script: `let ack='normal',unexpected=0;const calls=[];
const builds=${JSON.stringify({ [native.origin]: native.build_id, [tantivy.origin]: tantivy.build_id, [operator.origin]: operator.build_id })};
export default{fetch(r){const u=new URL(r.url);if(u.pathname==='/__fixture/control'){if(u.searchParams.has('ack'))ack=u.searchParams.get('ack');return Response.json({unexpected,calls})}
if(!(u.origin in builds)||u.pathname!=='/search'){unexpected++;return new Response('external traffic prohibited',{status:500})}
const p=u.searchParams;calls.push({origin:u.origin,q:p.get('q')??'',pds:p.get('pds'),preferred:p.get('prefer_pds'),preference:p.get('preference'),boost:p.get('boost'),ack});
const tv=u.origin===${JSON.stringify(tantivy.origin)},operator=u.origin===${JSON.stringify(operator.origin)};
const headers=new Headers({'Content-Type':'application/json','X-Typeahead-Engine':tv?'tantivy':'native','X-Typeahead-Build':builds[u.origin],'X-Typeahead-Format':tv?${JSON.stringify(tantivy.format)}:'FLATPX05'});
if(tv){headers.set('X-Typeahead-Binary-Sha256','c'.repeat(64));headers.set('X-Typeahead-Retrieval-Strategy','global-merge')}
if(p.has('pds'))headers.set('X-Typeahead-Pds',ack==='bad-pds'?'remote.test':p.get('pds'));
if(p.has('prefer_pds')){headers.set('X-Typeahead-Prefer-Pds',p.get('prefer_pds'));if(ack!=='bad-preference')headers.set('X-Typeahead-Preference',p.get('preference'));if(p.has('boost'))headers.set('X-Typeahead-Boost',ack==='bad-boost'?'00000000':'3e800000')}
const name=tv?'tantivy':operator?'native-operator':'native';let rows=[{did:'did:plc:remotefixture',handle:name+'-remote.test'},{did:'did:plc:localfixture',handle:name+'-local.test'}];
if(p.has('pds'))rows=rows.slice(1);else if(p.has('prefer_pds'))rows.reverse();return Response.json({actors:rows},{headers})}}` },
  ] });
  const publicOrigin = "https://public-rehearsal.test";
  const calls = (path: string, init?: Parameters<Miniflare["dispatchFetch"]>[1]) => mf.dispatchFetch(publicOrigin + path, init);
  async function control() {
    const response = await calls("/__fixture/route");
    return { etag: response.headers.get("ETag"), value: route.parse(await response.json()) };
  }
  async function update(value: object, etag: string | null) {
    const response = await calls("/__fixture/route", { method: "POST", headers: etag ? { "If-Match": etag } : {}, body: JSON.stringify(value) });
    await response.arrayBuffer(); return response.status;
  }
  async function origin(ack?: string) {
    const response = await calls(`/__fixture/origin${ack ? `?ack=${ack}` : ""}`);
    return stats.parse(await response.json());
  }
  async function generation(value: string) { await (await calls(`/__fixture/generation?value=${value}`)).arrayBuffer(); }
  const samples: { label: string; status: number; engine: string | null; build: string | null; cache: string; cache_generation: string | null; wall_ms: number }[] = [];
  async function search(path = aliases[0], query = "q=baseline") {
    const begin = performance.now(); const response = await calls(`${path}?${query}`);
    const text = await response.text();
    samples.push({ label: path + "?" + query, status: response.status, engine: response.headers.get("X-Typeahead-Engine"), build: response.headers.get("X-Typeahead-Build"), cache_generation: response.headers.get("X-Typeahead-Cache-Generation"), cache: response.headers.get("Server-Timing")?.includes('desc="hit"') ? "hit" : "miss", wall_ms: performance.now() - begin });
    return { response, text };
  }
  async function settled(path = aliases[0], query = "q=baseline") {
    for (let attempt = 0; attempt < 20; attempt++) {
      const result = await search(path, query);
      if (result.response.headers.get("Server-Timing")?.includes('desc="hit"')) return result;
      await Bun.sleep(10);
    }
    throw new Error("Local cache write did not settle");
  }
  const propagation: { label: string; cas_to_first_matching_uncached_response_ms: number; stale_uncached_responses: number }[] = [];
  async function observeTransition(engine: string, build: string, label: string, since: number) {
    let stale = 0;
    while (performance.now() - since < 2000) {
      const result = await search(aliases[0], `q=${label}-uncached-${stale}`);
      expect(result.response.status).toBe(200);
      if (result.response.headers.get("X-Typeahead-Engine") === engine && result.response.headers.get("X-Typeahead-Build") === build) {
        if (engine === "tantivy") { expect(result.response.headers.get("X-Typeahead-Format")).toBe(tantivy.format); expect(result.response.headers.get("X-Typeahead-Binary-Sha256")).toBe(tantivy.binary_sha256); expect(result.response.headers.get("X-Typeahead-Retrieval-Strategy")).toBe(tantivy.retrieval_strategy); }
        propagation.push({ label, cas_to_first_matching_uncached_response_ms: performance.now() - since, stale_uncached_responses: stale }); return;
      }
      stale++; await Bun.sleep(50);
    }
    throw new Error("Local transport identity did not converge within its retained-route bound");
  }
  async function correct(engine: string, build: string, query: string) {
    const result = await search(aliases[0], query);
    expect(result.response.status).toBe(200); expect(result.response.headers.get("X-Typeahead-Engine")).toBe(engine); expect(result.response.headers.get("X-Typeahead-Build")).toBe(build);
    return result;
  }
  async function cachedPair(engine: string, build: string) {
    await settled(); const before = await origin();
    const left = await settled(aliases[0]); const right = await settled(aliases[1]);
    expect(left.text).toBe(right.text); expect(left.response.headers.get("X-Typeahead-Engine")).toBe(engine); expect(left.response.headers.get("X-Typeahead-Build")).toBe(build); expect(right.response.headers.get("X-Typeahead-Engine")).toBe(engine); expect(right.response.headers.get("X-Typeahead-Build")).toBe(build);
    expect(await origin()).toEqual(before);
  }
  try {
    const db = await mf.getD1Database("DB", "public");
    await db.exec("CREATE TABLE metrics(hour INTEGER PRIMARY KEY, searches INTEGER DEFAULT 0,total_ms REAL DEFAULT 0,cache_hits INTEGER DEFAULT 0,cache_ms REAL DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER,PRIMARY KEY(bucket,domain));");
    expect(await update(native, null)).toBe(200); const initial = await control();
    await correct("native", native.build_id, "q=baseline"); await cachedPair("native", native.build_id);
    expect(await update(tantivy, initial.etag)).toBe(200); const promotedAt = performance.now();
    await observeTransition("tantivy", tantivy.build_id, "native-to-tantivy", promotedAt);
    const promoted = await control(); expect(promoted.value.native_rollback).toEqual(native);
    await cachedPair("native", native.build_id);
    await generation("B"); await correct("tantivy", tantivy.build_id, "q=baseline"); await cachedPair("tantivy", tantivy.build_id);
    for (const alias of aliases) {
      const hard = await search(alias, "q=hard&pds=https%3A%2F%2FLOCAL.test"); expect(hard.response.status).toBe(200); expect(actors.parse(JSON.parse(hard.text)).actors.map(actor => actor.did)).toEqual(["did:plc:localfixture"]);
      const combined = await search(alias, "q=combined&pds=local.test&prefer_pds=local.test&boost=0.25"); expect(combined.response.status).toBe(200); expect(actors.parse(JSON.parse(combined.text)).actors.map(actor => actor.did)).toEqual(["did:plc:localfixture"]);
      for (const mode of ["prefer_pds=local.test&preference=pds_first", "prefer_pds=local.test&boost=0.25"]) {
        const preferred = await search(alias, "q=preferred&" + mode); expect(preferred.response.status).toBe(200); expect(actors.parse(JSON.parse(preferred.text)).actors[0].did).toBe("did:plc:localfixture");
      }
    }
    for (const [ack, query] of [["bad-pds", "q=reject-pds&pds=local.test"], ["bad-preference", "q=reject-preference&prefer_pds=local.test&boost=0.25"], ["bad-boost", "q=reject-boost&prefer_pds=local.test&boost=0.25"]]) {
      await origin(ack); const rejected = await search(aliases[0], query); expect(rejected.response.status).toBe(503); expect(JSON.parse(rejected.text)).toMatchObject({ error: "PdsScopeUnavailable" });
      await origin("normal"); const recovered = await search(aliases[1], query); expect(recovered.response.status).toBe(200); expect(recovered.response.headers.get("Server-Timing")).toContain('desc="miss"');
    }
    expect(await update(native, initial.etag)).toBe(409); expect((await control()).value).toEqual(promoted.value);
    expect(await update(native, promoted.etag)).toBe(200); const compensatedAt = performance.now();
    await observeTransition("native", native.build_id, "tantivy-to-native", compensatedAt);
    await cachedPair("tantivy", tantivy.build_id);
    await generation("C"); await correct("native", native.build_id, "q=baseline"); await cachedPair("native", native.build_id);
    const compensation = await control(); expect(await update(operator, compensation.etag)).toBe(200); const operatorAt = performance.now();
    expect(await update(native, compensation.etag)).toBe(409); expect((await control()).value.current).toEqual(operator);
    await observeTransition("native", operator.build_id, "operator-native-change", operatorAt);
    await generation("D"); await correct("native", operator.build_id, "q=baseline"); await cachedPair("native", operator.build_id);
    const final = await origin(); expect(final.unexpected).toBe(0);
    expect(final.calls.some(call => call.pds === "local.test")).toBe(true); expect(final.calls.some(call => call.preference === "pds_first")).toBe(true); expect(final.calls.some(call => call.preference === "boost" && call.boost === "0.25")).toBe(true);
    expect(await fingerprints()).toEqual(sourceHashes);
    const evidence = { schema_version: 1, observed_at: new Date().toISOString(), source_sha256: sourceHashes, scope: "local-workerd-public-handler-transport-R2-CAS", passed: true, production_contact: false, release_qualified: false, production_downtime_measured: false, aliases, components: ["handleSearch", "search-transport.fetch", "conditionalRouteUpdate", "filesystem-backed Miniflare R2", "workerd Cache API", "local D1 metrics"], fixture_only: ["public alias dispatcher and cache-generation config control", "bounded deterministic origin responses; no native or Rust engine", "all Worker fetch egress confined to the local origin service binding", "scope acknowledgements validated upstream by actual handler"], checks: { native_tantivy_native_compensation: true, old_cache_identity_retained: true, transport_identity_verified_before_new_cache_generation: true, cache_generation_barrier: true, cache_hits_no_origin_work: true, hard_pds: true, pds_first_and_f32_boost: true, missing_or_wrong_scope_ack_rejected_without_cache_poisoning: true, stale_etags_rejected: true, operator_change_wins: true, external_origin_requests: final.unexpected }, wall_ms: performance.now() - started, propagation, public_requests: samples.length, intentional_scope_rejections: samples.filter(sample => sample.status === 503).length, origin_requests: final.calls.length, samples, limitations: ["One local fixture sequence with 50ms unique-key polling and a 2s test bound, not a latency benchmark or production propagation estimate", "No Cloudflare deployment/config propagation, real engines, source readiness, Fly network, release admission or downtime measured", "Alias paths enter the actual handler through a local dispatcher; production router covered separately by index tests"] };
    if (Bun.env.TYPEAHEAD_REHEARSAL_EVIDENCE) await writeFile(Bun.env.TYPEAHEAD_REHEARSAL_EVIDENCE, JSON.stringify(evidence, null, 2) + "\n");
  } finally { await mf.dispose(); await rm(directory, { recursive: true, force: true }); }
}, 30000);

import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { join, resolve } from "node:path";
import { z } from "zod";

export const aliases = ["/xrpc/tech.waow.typeahead.searchActors", "/xrpc/app.bsky.actor.searchActorsTypeahead"];
export const transportOrigin = "https://typeahead-search-transport.n8-3e9.workers.dev";
export const route = z.object({ version: z.literal(1), current: z.object({ origin: z.string(), build_id: z.string() }).passthrough(), previous: z.unknown().optional() });
export const actors = z.object({ actors: z.array(z.object({ did: z.string(), handle: z.string() })) });

const sourcePaths = ["src/handlers/search.ts", "src/handlers/search-contract.ts", "src/search-transport.ts", "src/search-route.ts", "src/validated-search.ts", "scripts/snapshot_refresh/route-rehearsal.ts", "scripts/snapshot_refresh/route-rehearsal.test.ts"];
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
return handleSearch(r,{...e,STATS_DB:e.DB,SEARCH_CACHE_GENERATION:generation},c)}}`);
  await writeFile(transportEntry, `import transport from ${JSON.stringify(resolve("src/search-transport.ts"))};
import {conditionalRouteUpdate,ROUTE_KEY} from ${JSON.stringify(resolve("src/search-route.ts"))};
export default{async fetch(r,e,c){if(new URL(r.url).pathname==='/__fixture/route'){
if(r.method==='POST'){const ok=await conditionalRouteUpdate(e.SEARCH_CONTROL,r.headers.get('If-Match'),await r.json());return Response.json({ok},{status:ok?200:409})}
const value=await e.SEARCH_CONTROL.get(ROUTE_KEY);return value?new Response(value.body,{headers:{ETag:value.etag}}):new Response(null,{status:404})}
return transport.fetch(r,e,c)}}`);
  async function bundle(entry: string) {
    const output = entry + ".mjs";
    execFileSync("bun", ["build", entry, "--target", "browser", "--external", "cloudflare:workers", "--outfile", output], { stdio: "pipe", timeout: 10000 });
    return readFile(output, "utf8");
  }
  return { publicScript: await bundle(publicEntry), transportScript: await bundle(transportEntry) };
}

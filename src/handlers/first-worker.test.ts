import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const MIGRATIONS = new URL("../../migrations/stats-d1/", import.meta.url).pathname;

// The engine answers each letter with three accounts named after it, and reports
// the most searches it held open at once as "peak"; "z" is
// down, so the Worker answers it from the Bluesky fallback.
const ENGINE = `let open=0,peak=0;export default {async fetch(r){const q=new URL(r.url).searchParams.get('q');
  if(q==='peak')return Response.json({actors:[{did:'did:plc:peak',handle:'peak.test',displayName:'peak '+peak}]});
  open++;peak=Math.max(peak,open);await new Promise(resolve=>setTimeout(resolve,20));open--;
  if(q==='z')return Response.json({error:'down'},{status:503});
  if(q==='nobody')return Response.json({actors:[]});
  if(q==='many'){const n=Math.min(Number(new URL(r.url).searchParams.get('limit')),25);return Response.json({actors:Array.from({length:n},(_,i)=>({did:'did:plc:many'+i,handle:'many'+i+'.test'}))});}
  return Response.json({actors:[1,2,3].map(n=>({did:'did:plc:'+q.replace(/[^a-z]/g,'')+n,handle:q.replace(/[^a-z]/g,'')+n+'.test',displayName:q+' '+n}))});}}`;
const BLUESKY = `export default {fetch(r){return Response.json({actors:[{did:'did:plc:fallback',handle:'fallback.test'}]});}}`;

test("/first renders live, keeps today's first places once, and keeps nothing for other queries", async () => {
  const directory = await mkdtemp(join(tmpdir(), "typeahead-first-"));
  const entry = join(directory, "worker.ts");
  const handler = new URL("./first.ts", import.meta.url).pathname;
  const first = new URL("../first.ts", import.meta.url).pathname;
  const stats = new URL("../stats/stats-store.ts", import.meta.url).pathname;
  await writeFile(entry, `import {handleFirst} from ${JSON.stringify(handler)}; import {keepToday} from ${JSON.stringify(first)}; import {statsDb} from ${JSON.stringify(stats)};
    export default {async fetch(r,env,ctx){if(new URL(r.url).pathname==='/cron')return Response.json(await keepToday(env,ctx,statsDb(env)));return handleFirst(r,env,ctx)}}`);
  const built = await Bun.build({ entrypoints: [entry], target: "browser" });
  if (!built.success) throw new Error(built.logs.join("\n"));
  const mf = new Miniflare({ workers: [
    { name: "public", modules: true, script: await built.outputs[0]!.text(), compatibilityDate: "2026-10-29", d1Databases: ["STATS_DB"],
      bindings: { SEARCH_BACKEND_URL: "https://backend.test", SEARCH_CACHE_GENERATION: "test-1" }, serviceBindings: { SEARCH_TRANSPORT: "engine" }, outboundService: "bluesky" },
    { name: "engine", modules: true, script: ENGINE },
    { name: "bluesky", modules: true, script: BLUESKY },
  ] });
  try {
    const d1 = await mf.getD1Database("STATS_DB");
    for (const name of (await readdir(MIGRATIONS)).filter((file) => file.endsWith(".sql")).sort()) {
      const sql = await Bun.file(join(MIGRATIONS, name)).text();
      for (const statement of sql.replace(/--[^\n]*/g, "").split(";").map((part) => part.trim()).filter(Boolean)) await d1.prepare(statement).run();
    }
    const get = (path: string) => mf.dispatchFetch(`https://typeahead.waow.tech${path}`, { redirect: "manual" });
    const rows = async (where = "") => (await d1.prepare(`SELECT query, rank, did FROM first_daily ${where} ORDER BY query, rank`).all<{ query: string; rank: number; did: string }>()).results;
    const settle = () => new Promise((resolve) => setTimeout(resolve, 200));

    const index = await get("/first");
    expect(index.status).toBe(200);
    expect(index.headers.get("Cache-Control")).toBe("public, max-age=60, s-maxage=600");
    const page = await index.text();
    expect(page.match(/class="tile"/g)).toHaveLength(26);
    expect(page).toContain('<meta property="og:image" content="https://typeahead.waow.tech/og-image/first">');
    expect(page).toContain("a 1");
    expect(page).toContain("@fallback.test");
    expect(page).toContain("1 day");
    await settle();
    const kept = await rows();
    expect(kept).toHaveLength(25 * 3);
    expect(kept.filter((row) => row.query === "z")).toEqual([]);
    expect(kept.find((row) => row.query === "a" && row.rank === 1)?.did).toBe("did:plc:a1");

    expect(await (await get("/cron")).json()).toBe(0);
    expect(await rows()).toHaveLength(25 * 3);

    const letter = await (await get("/first/A")).text();
    expect(letter).toContain("last 30 days");
    expect(letter.match(/<li><span class="rank">/g)).toHaveLength(3);
    expect(letter.match(/class="unkept"/g)).toHaveLength(29);

    const name = await (await get("/first/nate")).text();
    expect(name).toContain('<a class="crumb" href="/first">');
    expect(name).toContain('id="share"');
    expect(name).not.toContain("show 10 more");
    expect(name).toContain('content="https://typeahead.waow.tech/og-image/first?q=nate"');
    expect(name).toContain("nate 1");
    // the reader picks their own client, as on the homepage
    expect(name).toContain('href="https://aturi.to/did:plc:nate1"');
    expect(name).not.toContain("bsky.app/profile");
    expect(await (await get("/first/nobody")).text()).toContain("nobody comes up for this");
    const places = (page: string) => (page.match(/<span class="rank">/g) ?? []).length;
    const many = await (await get("/first/many")).text();
    expect(places(many)).toBe(10);
    expect(many).toContain('href="/first/many?n=20">show 10 more</a>');
    const more = await (await get("/first/many?n=20")).text();
    expect(places(more)).toBe(20);
    expect(more).toContain('href="/first/many?n=30"');
    expect(places(await (await get("/first/many?n=25")).text())).toBe(10);
    const last = await (await get("/first/many?n=30")).text();
    expect(places(last)).toBe(25);
    expect(last).not.toContain("show 10 more");
    expect(await (await get("/first/peak")).text()).toMatch(/peak [1-6]</);
    await settle();
    expect(await rows("WHERE query NOT IN ('a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y','z')")).toEqual([]);

    const redirect = await get("/first?q=%20Nate%20");
    expect(redirect.status).toBe(302);
    expect(redirect.headers.get("Location")).toBe("/first/nate");
    expect((await get("/first?q=%20")).headers.get("Location")).toBe("/first");
    expect((await get(`/first/${"x".repeat(65)}`)).headers.get("Location")).toBe("/first");

    const counted = await d1.prepare("SELECT hits FROM traffic_sources WHERE domain = 'typeahead-first'").first<{ hits: number }>();
    expect(counted?.hits).toBeGreaterThanOrEqual(26);
  } finally {
    await mf.dispose();
    await rm(directory, { recursive: true, force: true });
  }
});

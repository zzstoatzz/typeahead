import { afterEach, beforeEach, expect, setDefaultTimeout, setSystemTime, test } from "bun:test";
import { materializeStats, writeSnapshot } from "./enrichment";
import { statsBody } from "./handlers/stats";
import { knownSource } from "./handlers/source-icon";
import { recordActorDelta, recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { collectingCtx, statsStores } from "./stats-d1-fixture";
import { recordProfile } from "./stats-profile";
import { recordStats, statsMirror, statsMode, statsReadDb, writeStats } from "./stats-store";

// each test seeds two real stores, one of them a workerd process
setDefaultTimeout(60_000);

// The read switch (docs/plans/stats-off-turso.md, step 6): STATS_MODE picks
// the store every statistics read goes to. With the same rows in both
// stores the cron's KV values and the /stats page must not depend on it.

let stores: Awaited<ReturnType<typeof statsStores>>;

beforeEach(async () => {
  stores = await statsStores();
});

afterEach(async () => {
  setSystemTime();
  await stores.dispose();
});

function memoryKv() {
  const stored = new Map<string, string>();
  return {
    stored,
    async put(key: string, value: string) { stored.set(key, value); },
    // SAFETY: the page reads its four keys either as text or as JSON it
    // declares the shape of; this fake returns what the cron stored.
    get: (async (key: string, type?: string) => {
      const value = stored.get(key) ?? null;
      return type === "json" && value !== null ? JSON.parse(value) : value;
    }) as KVNamespace["get"],
  };
}

const NOW = Date.UTC(2026, 10, 12, 15, 7, 0);
const search = (client: string) =>
  new Request("https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate", { headers: { "X-Client": client } });

/** the same traffic in both stores, through the dual-write path, over two days */
async function fill() {
  const env = { STATS_DB: stores.d1, STATS_MODE: "read-turso" };
  for (let age = 0; age < 40; age++) {
    const at = NOW - age * 71 * 60_000;
    setSystemTime(new Date(at));
    const { ctx, tasks } = collectingCtx();
    const client = age % 3 === 0 ? "plyr.fm" : "leaflet.pub";
    recordStats(ctx, env, stores.turso, (store, now) => recordRequest(store, client, "search", age % 7 === 0 ? 503 : 200, now));
    recordStats(ctx, env, stores.turso, (store, now) => recordMetric(store, 10.1 + age * 0.3, now));
    recordStats(ctx, env, stores.turso, (store, now) => recordCacheHit(store, 0.7, now));
    recordStats(ctx, env, stores.turso, (store, now) => recordTrafficSource(store, search(client), now));
    recordStats(ctx, env, stores.turso, (store, now) => recordActorDelta(store, { actors: age + 1 }, now));
    await Promise.all(tasks);
    await writeStats(env, stores.turso, (store, now) =>
      writeSnapshot(store, { hour: Math.floor(now / 3_600_000), total: 9000 - age, with_handles: 8000 - age, with_avatars: 7000 - age, hidden: 10 }));
  }
  setSystemTime(new Date(NOW));
}

const VIEWS = [
  { apps: [], range: "7d", origin: "https://typeahead.waow.tech" },
  { apps: [], range: "1h", origin: "https://typeahead.waow.tech" },
  { apps: ["plyr.fm", "leaflet.pub"], range: "all", origin: "https://typeahead.waow.tech" },
];

test("one setting decides which stores are written and which is read", () => {
  const behaviour = (STATS_MODE: string | undefined) => {
    const env = { STATS_DB: stores.d1, STATS_MODE };
    return { mode: statsMode(env), writesD1: statsMirror(env) !== null, readsTurso: statsReadDb(env, stores.turso) === stores.turso };
  };
  expect(behaviour("turso-only")).toEqual({ mode: "turso-only", writesD1: false, readsTurso: true });
  expect(behaviour("read-turso")).toEqual({ mode: "read-turso", writesD1: true, readsTurso: true });
  expect(behaviour("read-d1")).toEqual({ mode: "read-d1", writesD1: true, readsTurso: false });
  expect(behaviour("d1-only")).toEqual({ mode: "d1-only", writesD1: true, readsTurso: false });
  for (const other of [undefined, "", "1", "d1", "READ-D1", "D1-ONLY"]) {
    expect(behaviour(other)).toEqual({ mode: "turso-only", writesD1: false, readsTurso: true });
  }
});

test("the deployed configuration names one of the modes", async () => {
  const config = await Bun.file(new URL("../wrangler.jsonc", import.meta.url).pathname).text();
  const modes = [...config.matchAll(/"STATS_MODE":\s*"([^"]*)"/g)].map((match) => match[1]);
  expect(modes).toHaveLength(1);
  expect<string>(statsMode({ STATS_MODE: modes[0] })).toBe(modes[0]!);
});

test("read-d1 without the binding fails instead of answering from Turso", () => {
  expect(() => statsReadDb({ STATS_MODE: "read-d1" }, stores.turso)).toThrow("STATS_MODE is read-d1 but this Worker has no STATS_DB binding");
  expect(statsReadDb({ STATS_MODE: "read-turso" }, stores.turso)).toBe(stores.turso);
});

test("the store that is read is the one whose rows come back", async () => {
  await stores.mirror.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-d1.example', 4)").run();
  await stores.turso.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-turso.example', 4)").run();
  const d1 = statsReadDb({ STATS_DB: stores.d1, STATS_MODE: "read-d1" }, stores.turso);
  const turso = statsReadDb({ STATS_DB: stores.d1, STATS_MODE: "read-turso" }, stores.turso);
  expect(await knownSource(d1, "only-in-d1.example")).toBe(true);
  expect(await knownSource(d1, "only-in-turso.example")).toBe(false);
  expect(await knownSource(turso, "only-in-d1.example")).toBe(false);
  expect(await knownSource(turso, "only-in-turso.example")).toBe(true);
});

test("while D1 is read, both stores are still written", async () => {
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1, STATS_MODE: "read-d1" }, stores.turso, (store, now) => recordRequest(store, "plyr.fm", "search", 200, now));
  await Promise.all(tasks);
  expect((await stores.mirror.prepare("SELECT hits FROM request_buckets").all()).results).toEqual([{ hits: 1 }]);
  expect((await stores.turso.prepare("SELECT hits FROM request_buckets").all()).results).toEqual([{ hits: 1 }]);
});

test("with d1-only, Turso is never asked", async () => {
  const env = { STATS_DB: stores.d1, STATS_MODE: "d1-only" };
  // a Turso handle that fails the test if anything touches it
  const untouchable = new Proxy(stores.turso, { get() { throw new Error("Turso was used under d1-only"); } });
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, env, untouchable, (store, now) => recordRequest(store, "plyr.fm", "search", 200, now));
  await Promise.all(tasks);
  await writeStats(env, untouchable, (store, now) =>
    writeSnapshot(store, { hour: Math.floor(now / 3_600_000), total: 9, with_handles: 8, with_avatars: 7, hidden: 1 }));
  expect(statsReadDb(env, untouchable)).not.toBe(untouchable);
  expect((await stores.mirror.prepare("SELECT hits FROM request_buckets").all()).results).toEqual([{ hits: 1 }]);
  expect((await stores.turso.prepare("SELECT count(*) AS n FROM request_buckets").all()).results).toEqual([{ n: 0 }]);
});

test("with d1-only, a missing binding and a failed cron write are not silent", async () => {
  const env = { STATS_MODE: "d1-only" };
  expect(() => statsReadDb(env, stores.turso)).toThrow("STATS_MODE is d1-only but this Worker has no STATS_DB binding");
  await expect(writeStats(env, stores.turso, async () => {})).rejects.toThrow("no STATS_DB binding");
  const failing = { STATS_DB: stores.d1, STATS_MODE: "d1-only" };
  await expect(writeStats(failing, stores.turso, async () => { throw new Error("d1 down"); })).rejects.toThrow("d1 down");
});

test("equal rows give the same KV values and the same page from either store", async () => {
  await fill();
  const kv = { turso: memoryKv(), d1: memoryKv() };
  for (const [source, STATS_MODE] of [["turso", "read-turso"], ["d1", "read-d1"]] as const) {
    const db = statsReadDb({ STATS_DB: stores.d1, STATS_MODE }, stores.turso);
    await materializeStats(db, { KV: kv[source] });
    await recordProfile(db, kv[source], NOW);
  }
  expect([...kv.d1.stored.keys()].sort()).toEqual(["request_data", "stats_data", "stats_profile"]);
  for (const key of kv.d1.stored.keys()) expect(kv.d1.stored.get(key)).toBe(kv.turso.stored.get(key)!);
  expect(JSON.parse(kv.d1.stored.get("stats_data")!).traffic_series_5m.length).toBeGreaterThan(30);

  // traffic after the cron ran: the page reads it live from the same store
  setSystemTime(new Date(NOW + 4 * 60_000));
  const { ctx, tasks } = collectingCtx();
  const env = { STATS_DB: stores.d1, STATS_MODE: "read-turso" };
  recordStats(ctx, env, stores.turso, (store, now) => recordTrafficSource(store, search("after-the-cron.example"), now));
  recordStats(ctx, env, stores.turso, (store, now) => recordRequest(store, "after-the-cron.example", "search", 200, now));
  await Promise.all(tasks);

  for (const view of VIEWS) {
    const fromTurso = await statsBody(stores.turso, { KV: kv.turso }, view);
    const fromD1 = await statsBody(stores.mirror, { KV: kv.d1 }, view);
    expect(fromD1.body).toBe(fromTurso.body);
    expect(fromD1.body).toContain("after-the-cron.example");
    expect(fromD1.body).toContain("leaflet.pub");
  }
});

test("a row only one store holds shows on that store's page alone", async () => {
  await fill();
  const kv = memoryKv();
  await materializeStats(stores.turso, { KV: kv });
  await stores.mirror.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-d1.example', 4)").run();
  expect((await statsBody(stores.mirror, { KV: kv }, VIEWS[0]!)).body).toContain("only-in-d1.example");
  expect((await statsBody(stores.turso, { KV: kv }, VIEWS[0]!)).body).not.toContain("only-in-d1.example");
});

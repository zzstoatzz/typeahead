import { afterEach, beforeEach, expect, setSystemTime, spyOn, test } from "bun:test";
import { materializeStats, writeSnapshot } from "./enrichment";
import { statsBody } from "./handlers/stats";
import { knownSource } from "./handlers/source-icon";
import { recordActorDelta, recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { collectingCtx, statsStores } from "./stats-d1-fixture";
import { recordProfile } from "./stats-profile";
import { recordStats, statsMirror, statsReadDb, statsReadSource, writeStats } from "./stats-store";

// The read switch (docs/plans/stats-off-turso.md, step 6): STATS_READ_SOURCE
// picks the store every statistics read goes to. With the same rows in both
// stores the cron's KV values and the /stats page must not depend on it.

let stores: Awaited<ReturnType<typeof statsStores>>;
let logged: string[];
let log: ReturnType<typeof spyOn>;

beforeEach(async () => {
  stores = await statsStores();
  logged = [];
  log = spyOn(console, "log").mockImplementation((line: string) => { logged.push(line); });
});

afterEach(async () => {
  setSystemTime();
  log.mockRestore();
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
  const env = { STATS_DB: stores.d1, STATS_DUAL_WRITE: "1" };
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

test("the setting picks the store, and only the value d1 with a binding picks D1", async () => {
  expect(statsReadSource({ STATS_DB: stores.d1, STATS_READ_SOURCE: "d1" })).toBe("d1");
  for (const STATS_READ_SOURCE of ["turso", "", "D1", "1", undefined]) {
    const env = { STATS_DB: stores.d1, STATS_READ_SOURCE };
    expect(statsReadSource(env)).toBe("turso");
    expect(statsReadDb(env, stores.turso)).toBe(stores.turso);
  }
  expect(logged).toEqual([]);

  expect(statsReadDb({ STATS_READ_SOURCE: "d1" }, stores.turso)).toBe(stores.turso);
  expect(logged.map((line) => JSON.parse(line))).toEqual([{ event: "stats_read_source_unavailable", wanted: "d1", using: "turso" }]);
});

test("the store that is read is the one whose rows come back", async () => {
  await stores.mirror.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-d1.example', 4)").run();
  await stores.turso.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-turso.example', 4)").run();
  const d1 = statsReadDb({ STATS_DB: stores.d1, STATS_READ_SOURCE: "d1" }, stores.turso);
  const turso = statsReadDb({ STATS_DB: stores.d1, STATS_READ_SOURCE: "turso" }, stores.turso);
  expect(await knownSource(d1, "only-in-d1.example")).toBe(true);
  expect(await knownSource(d1, "only-in-turso.example")).toBe(false);
  expect(await knownSource(turso, "only-in-d1.example")).toBe(false);
  expect(await knownSource(turso, "only-in-turso.example")).toBe(true);
});

test("reading D1 keeps D1 written even with dual write switched off", async () => {
  expect(statsMirror({ STATS_DB: stores.d1, STATS_DUAL_WRITE: "0", STATS_READ_SOURCE: "d1" })).not.toBeNull();
  expect(statsMirror({ STATS_DUAL_WRITE: "0", STATS_READ_SOURCE: "d1" })).toBeNull();
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1, STATS_DUAL_WRITE: "0", STATS_READ_SOURCE: "d1" }, stores.turso,
    (store, now) => recordRequest(store, "plyr.fm", "search", 200, now));
  await Promise.all(tasks);
  expect((await stores.mirror.prepare("SELECT hits FROM request_buckets").all()).results).toEqual([{ hits: 1 }]);
  expect((await stores.turso.prepare("SELECT hits FROM request_buckets").all()).results).toEqual([{ hits: 1 }]);
});

test("equal rows give the same KV values and the same page from either store", async () => {
  await fill();
  const kv = { turso: memoryKv(), d1: memoryKv() };
  for (const source of ["turso", "d1"] as const) {
    const db = statsReadDb({ STATS_DB: stores.d1, STATS_READ_SOURCE: source }, stores.turso);
    await materializeStats(db, { KV: kv[source] });
    await recordProfile(db, kv[source], NOW);
  }
  expect([...kv.d1.stored.keys()].sort()).toEqual(["request_data", "stats_data", "stats_profile"]);
  for (const key of kv.d1.stored.keys()) expect(kv.d1.stored.get(key)).toBe(kv.turso.stored.get(key)!);
  expect(JSON.parse(kv.d1.stored.get("stats_data")!).traffic_series_5m.length).toBeGreaterThan(30);

  // traffic after the cron ran: the page reads it live from the same store
  setSystemTime(new Date(NOW + 4 * 60_000));
  const { ctx, tasks } = collectingCtx();
  const env = { STATS_DB: stores.d1, STATS_DUAL_WRITE: "1" };
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

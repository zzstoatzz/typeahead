import { afterEach, beforeEach, expect, setDefaultTimeout, spyOn, test } from "bun:test";
import type { SqlDb } from "./db";
import { recordSnapshot, writeSnapshot } from "./enrichment";
import { recordActorDelta, recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { collectingCtx, statsRows, statsStores } from "./stats-d1-fixture";
import { recordStats, statsMirror, writeStats, type StatsWrite } from "./stats-store";

// each test fills a workerd-backed D1, which a loaded machine makes slow
setDefaultTimeout(60_000);

// Dual write (docs/plans/stats-off-turso.md, step 2): every statistics writer
// goes to Turso and to D1 independently. The stores must end up with the same
// rows, and neither may be able to stop the other or reach the caller.

let stores: Awaited<ReturnType<typeof statsStores>>;
let logged: string[];
let log: ReturnType<typeof spyOn>;

beforeEach(async () => {
  stores = await statsStores();
  logged = [];
  log = spyOn(console, "log").mockImplementation((line: string) => { logged.push(line); });
});

afterEach(async () => {
  log.mockRestore();
  await stores.dispose();
});

const search = new Request("https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate", { headers: { "X-Client": "plyr.fm" } });

const WRITERS: [string, StatsWrite][] = [
  ["recordRequest", (store, now) => recordRequest(store, "plyr.fm", "search", 200, now)],
  ["recordRequest, another status", (store, now) => recordRequest(store, "plyr.fm", "search", 503, now)],
  ["recordMetric", (store, now) => recordMetric(store, 12.5, now)],
  ["recordCacheHit", (store, now) => recordCacheHit(store, 1.5, now)],
  ["recordTrafficSource", (store, now) => recordTrafficSource(store, search, now)],
  ["recordActorDelta, new actors", (store, now) => recordActorDelta(store, { actors: 3 }, now)],
  ["recordActorDelta, deletes", (store, now) => recordActorDelta(store, { actors: -1 }, now)],
  ["writeSnapshot", (store, now) => writeSnapshot(store, { hour: Math.floor(now / 3_600_000), total: 90, with_handles: 80, with_avatars: 70, hidden: 10 })],
];

const failing: SqlDb = {
  prepare() { throw new Error("turso is down"); },
  async batch() { throw new Error("turso is down"); },
};

const rowCount = (rows: Awaited<ReturnType<typeof statsRows>>, table: string) => rows.find((entry) => entry.table === table)?.rows.length;

test("every writer leaves the same rows in both stores, after the response and inline", async () => {
  const env = { STATS_DB: stores.d1, STATS_MODE: "read-turso" };
  for (const [, write] of WRITERS) {
    for (let i = 0; i < 2; i++) {
      const { ctx, tasks } = collectingCtx();
      recordStats(ctx, env, stores.turso, write);
      expect(tasks).toHaveLength(2);
      await Promise.all(tasks);
      await writeStats(env, stores.turso, write);
    }
  }
  const turso = await statsRows(stores.turso);
  expect(await statsRows(stores.mirror)).toEqual(turso);
  expect(turso.map((entry) => [entry.table, entry.rows.length])).toEqual([
    ["actor_deltas", 1], ["counters", 1], ["metrics", 1], ["request_buckets", 2], ["request_daily", 0],
    ["snapshots", 1], ["traffic_buckets", 1], ["traffic_daily", 0], ["traffic_sources", 1],
  ]);
  expect(turso.find((entry) => entry.table === "counters")?.rows).toEqual([{ name: "actors_total", value: 8 }]);
  expect(logged).toEqual([]);
});

test("both stores file a write under the bucket of one shared clock reading", async () => {
  const seen: number[] = [];
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1, STATS_MODE: "read-turso" }, stores.turso, async (_store, now) => { seen.push(now); });
  await Promise.all(tasks);
  expect(seen).toHaveLength(2);
  expect(seen[0]).toBe(seen[1]);
});

test("a failing D1 write does not stop the Turso write or reject the response's tasks", async () => {
  await stores.d1.prepare("DROP TABLE request_buckets").run();
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1, STATS_MODE: "read-turso" }, stores.turso, WRITERS[0]![1]);
  expect(await Promise.all(tasks)).toEqual([undefined, undefined]);
  expect(rowCount(await statsRows(stores.turso), "request_buckets")).toBe(1);
  expect(logged.map((line) => JSON.parse(line))).toMatchObject([{ event: "stats_write_failed", store: "d1" }]);
});

test("a failing Turso write does not stop the D1 write or reject the response's tasks", async () => {
  for (const turso of [() => failing, () => Promise.reject<SqlDb>(new Error("no client"))]) {
    const { ctx, tasks } = collectingCtx();
    recordStats(ctx, { STATS_DB: stores.d1, STATS_MODE: "read-turso" }, turso(), WRITERS[0]![1]);
    expect(await Promise.all(tasks)).toEqual([undefined, undefined]);
  }
  expect((await statsRows(stores.mirror)).find((entry) => entry.table === "request_buckets")?.rows).toMatchObject([{ hits: 2 }]);
  expect(logged.map((line) => JSON.parse(line))).toMatchObject([
    { event: "stats_write_failed", store: "turso", error: "turso is down" },
    { event: "stats_write_failed", store: "turso", error: "no client" },
  ]);
});

test("inline, a Turso failure reaches the caller after D1 is written; a D1 failure never does", async () => {
  const env = { STATS_DB: stores.d1, STATS_MODE: "read-turso" };
  await expect(writeStats(env, failing, WRITERS[2]![1])).rejects.toThrow("turso is down");
  expect(rowCount(await statsRows(stores.mirror), "metrics")).toBe(1);

  await stores.d1.prepare("DROP TABLE metrics").run();
  await writeStats(env, stores.turso, WRITERS[2]![1]);
  expect(rowCount(await statsRows(stores.turso), "metrics")).toBe(1);
  expect(logged.map((line) => JSON.parse(line))).toMatchObject([{ event: "stats_write_failed", store: "d1" }]);
});

test("with the switch off no D1 statement is issued", async () => {
  for (const STATS_MODE of ["turso-only", "", "1", undefined]) {
    const env = { STATS_DB: stores.d1, STATS_MODE };
    expect(statsMirror(env)).toBeNull();
    for (const [, write] of WRITERS) {
      const { ctx, tasks } = collectingCtx();
      recordStats(ctx, env, stores.turso, write);
      expect(tasks).toHaveLength(1);
      await Promise.all(tasks);
      await writeStats(env, stores.turso, write);
    }
  }
  const mirror = await statsRows(stores.mirror);
  expect(mirror.map((entry) => entry.rows)).toEqual(mirror.map((entry) => entry.table === "counters" ? [{ name: "actors_total", value: 0 }] : []));
  expect(rowCount(await statsRows(stores.turso), "request_buckets")).toBe(2);
});

test("without the binding the D1 path is a no-op", async () => {
  const env = { STATS_MODE: "read-turso" };
  expect(statsMirror(env)).toBeNull();
  for (const [, write] of WRITERS) {
    const { ctx, tasks } = collectingCtx();
    recordStats(ctx, env, stores.turso, write);
    expect(tasks).toHaveLength(1);
    await Promise.all(tasks);
    await writeStats({}, stores.turso, write);
  }
  expect(rowCount(await statsRows(stores.turso), "request_buckets")).toBe(2);
  expect(logged).toEqual([]);
});

test("the hourly snapshot counts actors in Turso and writes the row to both stores", async () => {
  await stores.client.executeMultiple(`
    CREATE TABLE actors (did TEXT PRIMARY KEY, handle TEXT NOT NULL DEFAULT '', avatar_url TEXT DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0);
    INSERT INTO actors (did, handle, avatar_url, hidden) VALUES ('did:plc:a', 'a.test', 'cid', 0), ('did:plc:b', '', '', 0), ('did:plc:c', 'c.test', '', 1);
    UPDATE counters SET value = 3 WHERE name = 'actors_total';
  `);
  await recordSnapshot(stores.turso, { STATS_DB: stores.d1, STATS_MODE: "read-turso" });
  const snapshots = (await statsRows(stores.turso)).find((entry) => entry.table === "snapshots")?.rows;
  expect(snapshots).toMatchObject([{ total: 2, with_handles: 2, with_avatars: 1, hidden: 1 }]);
  expect((await statsRows(stores.mirror)).find((entry) => entry.table === "snapshots")?.rows).toEqual(snapshots);
});

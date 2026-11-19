import { afterEach, beforeEach, expect, setDefaultTimeout, spyOn, test } from "bun:test";
import { writeSnapshot } from "./enrichment";
import { recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { collectingCtx, statsRows, statsStores } from "./stats-d1-fixture";
import { recordStats, statsDb, writeStats, type StatsWrite } from "./stats-store";

// each test fills a workerd-backed D1, which a loaded machine makes slow
setDefaultTimeout(60_000);

// Every statistics writer goes through recordStats (after the response) or
// writeStats (the cron), and both write the D1 statistics database.

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
  ["writeSnapshot", (store, now) => writeSnapshot(store, { hour: Math.floor(now / 3_600_000), total: 90, with_handles: 80, with_avatars: 70, hidden: 10 })],
];

const rowsOf = (rows: Awaited<ReturnType<typeof statsRows>>, table: string) => rows.find((entry) => entry.table === table)?.rows;

test("every writer leaves its rows in the statistics database, after the response and inline", async () => {
  const env = { STATS_DB: stores.d1 };
  for (const [, write] of WRITERS) {
    for (let i = 0; i < 2; i++) {
      const { ctx, tasks } = collectingCtx();
      recordStats(ctx, env, write);
      expect(tasks).toHaveLength(1);
      await Promise.all(tasks);
      await writeStats(env, write);
    }
  }
  const rows = await statsRows(stores.db);
  expect(rows.map((entry) => [entry.table, entry.rows.length])).toEqual([
    ["actor_deltas", 0], ["counters", 1], ["metrics", 1], ["request_buckets", 2], ["request_daily", 0],
    ["snapshots", 1], ["traffic_buckets", 1], ["traffic_daily", 0], ["traffic_sources", 1],
  ]);
  expect(rowsOf(rows, "request_buckets")).toMatchObject([
    { domain: "plyr.fm", endpoint: "search", status: 200, hits: 4 },
    { domain: "plyr.fm", endpoint: "search", status: 503, hits: 4 },
  ]);
  expect(rowsOf(rows, "metrics")).toMatchObject([{ searches: 4, total_ms: 50, cache_hits: 4, cache_ms: 6 }]);
  expect(rowsOf(rows, "traffic_sources")).toEqual([{ domain: "plyr.fm", hits: 4 }]);
  expect(rowsOf(rows, "snapshots")).toMatchObject([{ total: 90, with_handles: 80, with_avatars: 70, hidden: 10 }]);
  expect(logged).toEqual([]);
});

test("a write is handed the store and the clock reading taken when it was recorded", async () => {
  const seen: number[] = [];
  const before = Date.now();
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1 }, async (_store, now) => { seen.push(now); });
  const after = Date.now();
  await Promise.all(tasks);
  await writeStats({ STATS_DB: stores.d1 }, async (_store, now) => { seen.push(now); });
  expect(seen).toHaveLength(2);
  expect(seen[0]).toBeGreaterThanOrEqual(before);
  expect(seen[0]).toBeLessThanOrEqual(after);
  expect(seen[1]).toBeGreaterThanOrEqual(after);
});

test("a failing write after the response is logged and does not reject the response's tasks", async () => {
  await stores.d1.prepare("DROP TABLE request_buckets").run();
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, { STATS_DB: stores.d1 }, WRITERS[0]![1]);
  recordStats(ctx, { STATS_DB: stores.d1 }, async () => { throw new Error("d1 down"); });
  expect(await Promise.all(tasks)).toEqual([undefined, undefined]);
  // the write that throws at once is logged before the one D1 refuses
  expect(logged.map((line) => JSON.parse(line))).toMatchObject([
    { event: "stats_write_failed", store: "d1", error: "d1 down" },
    { event: "stats_write_failed", store: "d1", error: expect.stringContaining("request_buckets") },
  ]);
});

test("inline, a failure reaches the caller", async () => {
  const env = { STATS_DB: stores.d1 };
  await expect(writeStats(env, async () => { throw new Error("d1 down"); })).rejects.toThrow("d1 down");
  await stores.d1.prepare("DROP TABLE metrics").run();
  await expect(writeStats(env, WRITERS[2]![1])).rejects.toThrow();
  expect(logged).toEqual([]);
});

test("a missing binding is not silent", async () => {
  expect(() => statsDb({})).toThrow("this Worker has no STATS_DB binding");
  await expect(writeStats({}, async () => {})).rejects.toThrow("this Worker has no STATS_DB binding");

  let wrote = false;
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, {}, async () => { wrote = true; });
  expect(await Promise.all(tasks)).toEqual([undefined]);
  expect(wrote).toBe(false);
  expect(logged.map((line) => JSON.parse(line))).toEqual([
    { event: "stats_write_failed", store: "d1", error: "this Worker has no STATS_DB binding" },
  ]);
});

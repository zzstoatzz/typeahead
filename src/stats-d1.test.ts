import { afterAll, afterEach, expect, setDefaultTimeout, setSystemTime, test } from "bun:test";
import { rowsAs } from "./db";
import { materializeStats, writeSnapshot } from "./enrichment";
import { knownSource } from "./handlers/source-icon";
import { statsBody } from "./handlers/stats";
import { recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { PROFILE_KEY, parseProfile, recordProfile } from "./stats-profile";
import { STATS_TABLES, collectingCtx, statsStores } from "./stats-d1-fixture";
import { recordStats, writeStats } from "./stats-store";
import { sourceQueries, tailQueries } from "./stats-tail";

// each test fills a workerd-backed D1, which a loaded machine makes slow
setDefaultTimeout(60_000);

// The D1 database is created by the migrations alone, so a statement the
// statistics code issues against a table or column the migrations lack has to
// fail here rather than in production.

const { d1, db, dispose } = await statsStores();
afterAll(dispose);
afterEach(() => { setSystemTime(); });

const stored = new Map<string, string>();
const kv = { async put(key: string, value: string) { stored.set(key, value); } };
const storedJson = (key: string) => JSON.parse(stored.get(key) ?? "null");

test("the migrations define the nine statistics tables and seed the actor counter", async () => {
  const { results } = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name != 'd1_migrations' ORDER BY name",
  ).all<{ name: string }>();
  expect(results.map((r) => r.name)).toEqual(STATS_TABLES);
  expect(await db.prepare("SELECT value FROM counters WHERE name = 'actors_total'").first<{ value: number }>()).toEqual({ value: 0 });
});

test("every statistics statement runs against the migrated database", async () => {
  const now = Date.now();
  const bucket = Math.floor(now / 300_000);
  const search = (client: string) =>
    new Request("https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate", { headers: { "X-Client": client } });

  for (let i = 0; i < 2; i++) {
    await recordRequest(db, "plyr.fm", "search", 200, now);
    await recordMetric(db, 12.5, now);
    await recordCacheHit(db, 1.5, now);
    await recordTrafficSource(db, search("plyr.fm"), now);
  }
  await recordRequest(db, "plyr.fm", "search", 503, now);
  await recordTrafficSource(db, search("smoke-testing"), now);
  // nothing in the Worker writes actor deltas; the rows the table holds are still read
  await db.prepare("INSERT INTO actor_deltas (bucket, actors_delta, handles_delta, avatars_delta) VALUES (?1, 5, 0, 0)").bind(bucket).run();
  await writeSnapshot(db, { hour: Math.floor(now / 3_600_000), total: 90, with_handles: 80, with_avatars: 70, hidden: 10 });
  await writeSnapshot(db, { hour: Math.floor(now / 3_600_000), total: 95, with_handles: 80, with_avatars: 70, hidden: 5 });

  expect(await knownSource(db, "plyr.fm")).toBe(true);
  expect(await knownSource(db, "never-seen.example")).toBe(false);

  // 100 days back: past the 90-day window, so the cron rolls these into the
  // daily tables and deletes them in one transaction.
  const old = bucket - 100 * 288;
  await db.batch([
    db.prepare("INSERT INTO traffic_buckets (bucket, domain, hits) VALUES (?1, 'plyr.fm', 7)").bind(old),
    db.prepare("INSERT INTO request_buckets (bucket, domain, endpoint, status, hits) VALUES (?1, 'plyr.fm', 'search', 200, 9)").bind(old),
  ]);

  // twice: the lazily issued CREATE TABLE IF NOT EXISTS for the daily tables
  // must be a no-op on tables the migrations made, and a rolled-up bucket must
  // not be added again.
  await materializeStats(db, { KV: kv });
  await materializeStats(db, { KV: kv });

  expect(storedJson("stats_data")).toMatchObject({
    metrics: [{ hour: bucket, searches: 2, total_ms: 25, cache_hits: 2, cache_ms: 3 }],
    snapshots: [{ hour: Math.floor(now / 3_600_000), total: 95, with_handles: 80, with_avatars: 70, hidden: 5 }],
    deltas: [{ bucket, actors_delta: 5, handles_delta: 0, avatars_delta: 0 }],
    traffic_series: [
      { hour: Math.floor(bucket / 12), domain: "plyr.fm", hits: 2 },
      { hour: Math.floor(bucket / 12), domain: "smoke-testing", hits: 1 },
    ],
    traffic_series_5m: [
      { bucket, domain: "plyr.fm", hits: 2 },
      { bucket, domain: "smoke-testing", hits: 1 },
    ],
    traffic_daily: [
      { day: Math.floor(old / 288), domain: "plyr.fm", hits: 7 },
      { day: Math.floor(bucket / 288), domain: "plyr.fm", hits: 2 },
      { day: Math.floor(bucket / 288), domain: "smoke-testing", hits: 1 },
    ],
    traffic_sources_total: 1,
  });
  expect(storedJson("request_data")).toMatchObject({
    series: [
      { hour: Math.floor(bucket / 12), domain: "plyr.fm", endpoint: "search", status: 200, hits: 2 },
      { hour: Math.floor(bucket / 12), domain: "plyr.fm", endpoint: "search", status: 503, hits: 1 },
    ],
    series_5m: [
      { bucket, domain: "plyr.fm", endpoint: "search", status: 200, hits: 2 },
      { bucket, domain: "plyr.fm", endpoint: "search", status: 503, hits: 1 },
    ],
    daily: [
      { day: Math.floor(old / 288), domain: "plyr.fm", endpoint: "search", status: 200, hits: 9 },
      { day: Math.floor(bucket / 288), domain: "plyr.fm", endpoint: "search", status: 200, hits: 2 },
      { day: Math.floor(bucket / 288), domain: "plyr.fm", endpoint: "search", status: 503, hits: 1 },
    ],
  });

  const [trafficDaily, requestDaily, oldTraffic, oldRequests] = await db.batch([
    db.prepare("SELECT day, domain, hits FROM traffic_daily"),
    db.prepare("SELECT day, domain, endpoint, status, hits FROM request_daily"),
    db.prepare("SELECT count(*) AS n FROM traffic_buckets WHERE bucket = ?1").bind(old),
    db.prepare("SELECT count(*) AS n FROM request_buckets WHERE bucket = ?1").bind(old),
  ], "read");
  expect(trafficDaily.results).toEqual([{ day: Math.floor(old / 288), domain: "plyr.fm", hits: 7 }]);
  expect(requestDaily.results).toEqual([{ day: Math.floor(old / 288), domain: "plyr.fm", endpoint: "search", status: 200, hits: 9 }]);
  expect(rowsAs<{ n: number }>(oldTraffic.results)).toEqual([{ n: 0 }]);
  expect(rowsAs<{ n: number }>(oldRequests.results)).toEqual([{ n: 0 }]);

  const profile = await recordProfile(db, kv, now + 2 * 86_400_000);
  expect(parseProfile(stored.get(PROFILE_KEY) ?? null)).toEqual(profile);

  const [sources, sourceTotals, metricsTail, traffic5m, trafficHour, request5m, requestHour] = await db.batch([
    ...sourceQueries(db),
    ...tailQueries(db, bucket, bucket),
  ], "read");
  expect(sources.results).toEqual([{ domain: "plyr.fm", hits: 2 }]);
  expect(sourceTotals.results).toEqual([{ domains: 1, hits: 2 }]);
  expect(metricsTail.results).toHaveLength(1);
  expect(traffic5m.results).toHaveLength(2);
  expect(trafficHour.results).toHaveLength(2);
  expect(request5m.results).toHaveLength(2);
  expect(requestHour.results).toHaveLength(2);
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

const VIEWS = [
  { apps: [], range: "7d", origin: "https://typeahead.waow.tech" },
  { apps: [], range: "1h", origin: "https://typeahead.waow.tech" },
  { apps: ["plyr.fm", "leaflet.pub"], range: "all", origin: "https://typeahead.waow.tech" },
];

test("the page shows what the cron materialized and, live, what was recorded after it", async () => {
  for (const table of STATS_TABLES.filter((name) => name !== "counters")) await db.prepare(`DELETE FROM ${table}`).run();
  const NOW = Date.UTC(2026, 10, 12, 15, 7, 0);
  const env = { STATS_DB: d1 };
  const from = (client: string) =>
    new Request("https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate", { headers: { "X-Client": client } });

  // two days of traffic through the writers the Worker uses
  for (let age = 0; age < 40; age++) {
    setSystemTime(new Date(NOW - age * 71 * 60_000));
    const { ctx, tasks } = collectingCtx();
    const client = age % 3 === 0 ? "plyr.fm" : "leaflet.pub";
    recordStats(ctx, env, (store, now) => recordRequest(store, client, "search", age % 7 === 0 ? 503 : 200, now));
    recordStats(ctx, env, (store, now) => recordMetric(store, 10.1 + age * 0.3, now));
    recordStats(ctx, env, (store, now) => recordCacheHit(store, 0.7, now));
    recordStats(ctx, env, (store, now) => recordTrafficSource(store, from(client), now));
    await Promise.all(tasks);
    await writeStats(env, (store, now) =>
      writeSnapshot(store, { hour: Math.floor(now / 3_600_000), total: 9000 - age, with_handles: 8000 - age, with_avatars: 7000 - age, hidden: 10 }));
  }
  setSystemTime(new Date(NOW));

  const kv = memoryKv();
  await materializeStats(db, { KV: kv });
  await recordProfile(db, kv, NOW);
  expect([...kv.stored.keys()].sort()).toEqual(["request_data", "stats_data", "stats_profile"]);
  const materialized = JSON.parse(kv.stored.get("stats_data")!);
  expect(materialized.traffic_series_5m.length).toBeGreaterThan(30);
  expect(materialized.snapshots).toHaveLength(40);
  expect(materialized.snapshots.at(-1)).toEqual({ hour: Math.floor(NOW / 3_600_000), total: 9000, with_handles: 8000, with_avatars: 7000, hidden: 10 });

  const before = await statsBody(db, { KV: kv }, VIEWS[0]!);
  expect(before.body).toContain("leaflet.pub");
  expect(before.body).not.toContain("after-the-cron.example");

  // traffic after the cron ran is not in KV: the page reads it from the store
  setSystemTime(new Date(NOW + 4 * 60_000));
  const { ctx, tasks } = collectingCtx();
  recordStats(ctx, env, (store, now) => recordTrafficSource(store, from("after-the-cron.example"), now));
  recordStats(ctx, env, (store, now) => recordRequest(store, "after-the-cron.example", "search", 200, now));
  await Promise.all(tasks);

  for (const view of VIEWS) {
    const page = await statsBody(db, { KV: kv }, view);
    expect(page.body).toContain("after-the-cron.example");
    expect(page.body).toContain("leaflet.pub");
    expect(page.timing).toMatch(/^query;dur=\d+, process;dur=\d+, render;dur=\d+, total;dur=\d+$/);
  }
});

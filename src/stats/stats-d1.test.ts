import { afterAll, afterEach, expect, setDefaultTimeout, setSystemTime, test } from "bun:test";
import { rowsAs } from "../db";
import { materializeStats, readRequestPayload, readStatsPayload, writeSnapshot, type RequestPayload, type StatsPayload } from "./enrichment";
import { knownSource } from "../handlers/source-icon";
import { statsBody } from "../handlers/stats";
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
const kv = {
  async get(key: string) { return stored.get(key) ?? null; },
  async put(key: string, value: string) { stored.set(key, value); },
};
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

// A run that carries the previous copy forward has to write what a run that
// reads everything would. The differences it could introduce are at the
// seams: the hour it resumes from, a count that lands after the previous run
// read, the trailing edge of each window, and a bucket rolled into the daily
// table between the two runs.
test("an hourly run that carries the previous copy writes what a full read returns", async () => {
  for (const table of STATS_TABLES.filter((name) => name !== "counters")) await db.prepare(`DELETE FROM ${table}`).run();
  const T0 = Date.UTC(2026, 10, 12, 15, 0, 2);
  const bucket0 = Math.floor(T0 / 300_000);
  let seed = 7;
  const rand = (n: number) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
  const apps = ["plyr.fm", "leaflet.pub", "pckt.blog"];
  const fill = async (buckets: number[]) => {
    const stmts = buckets.flatMap((bucket) => {
      const app = apps[rand(apps.length)]!, status = rand(9) === 0 ? 503 : 200, hits = 1 + rand(5);
      return [
        db.prepare("INSERT INTO traffic_buckets (bucket, domain, hits) VALUES (?1, ?2, ?3) ON CONFLICT(bucket, domain) DO UPDATE SET hits = hits + excluded.hits").bind(bucket, app, hits),
        db.prepare("INSERT INTO request_buckets (bucket, domain, endpoint, status, hits) VALUES (?1, ?2, 'search', ?3, ?4) ON CONFLICT(bucket, domain, endpoint, status) DO UPDATE SET hits = hits + excluded.hits").bind(bucket, app, status, hits),
        db.prepare("INSERT INTO metrics (hour, searches, total_ms) VALUES (?1, ?2, 1.5) ON CONFLICT(hour) DO UPDATE SET searches = searches + excluded.searches").bind(bucket, hits),
      ];
    });
    for (let i = 0; i < stmts.length; i += 90) await db.batch(stmts.slice(i, i + 90));
  };
  // 92 days of history, dense at both edges of every window the payload has
  const history = [
    ...Array.from({ length: 400 }, () => bucket0 - rand(92 * 288)),
    ...Array.from({ length: 80 }, () => bucket0 - 90 * 288 + rand(30) - 15),
    ...Array.from({ length: 80 }, () => bucket0 - 30 * 288 + rand(30) - 15),
    ...Array.from({ length: 80 }, () => bucket0 - 2 * 288 + rand(30) - 15),
    ...Array.from({ length: 80 }, () => bucket0 - rand(30)),
  ];
  await fill(history);
  for (let h = 0; h < 6; h++) await writeSnapshot(db, { hour: Math.floor(T0 / 3_600_000) - h, total: 100 - h, with_handles: 90, with_avatars: 80, hidden: 1 });

  const kv = memoryKv();
  const stamps = (key: string) => { const { materialized_at, rebuilt_at } = JSON.parse(kv.stored.get(key)!); return { materialized_at, rebuilt_at }; };
  const written = (key: string) => { const { materialized_at: _m, rebuilt_at: _r, ...payload } = JSON.parse(kv.stored.get(key)!); return sortedPayload(payload); };

  await materializeStats(db, { KV: kv }, T0);
  expect(stamps("stats_data")).toEqual({ materialized_at: T0, rebuilt_at: T0 });

  for (let run = 1; run <= 3; run++) {
    const now = T0 + run * 3_600_000;
    const nowBucket = Math.floor(now / 300_000);
    // the hour since the last run, and counts that landed in the hour before it after that run read
    await fill([...Array.from({ length: 40 }, () => nowBucket - rand(12)), nowBucket - 12, nowBucket - 13, nowBucket - 13]);
    await writeSnapshot(db, { hour: Math.floor(now / 3_600_000), total: 200 + run, with_handles: 90, with_avatars: 80, hidden: 1 });
    await materializeStats(db, { KV: kv }, now);
    expect(stamps("stats_data")).toEqual({ materialized_at: now, rebuilt_at: T0 });
    expect(stamps("request_data")).toEqual({ materialized_at: now, rebuilt_at: T0 });
    expect(written("stats_data")).toEqual(sortedPayload(await readStatsPayload(db, now)));
    expect(written("request_data")).toEqual(sortedPayload(await readRequestPayload(db, now)));
  }
  expect(JSON.parse(kv.stored.get("request_data")!).daily.length).toBeGreaterThan(100);

  // a day after the last full read, everything is read again
  const later = T0 + 24 * 3_600_000;
  await materializeStats(db, { KV: kv }, later);
  expect(stamps("stats_data")).toEqual({ materialized_at: later, rebuilt_at: later });
  expect(stamps("request_data")).toEqual({ materialized_at: later, rebuilt_at: later });
});

test("a previous copy that cannot be carried is replaced by a full read", async () => {
  const now = Date.UTC(2026, 10, 14, 9, 0, 1);
  const full = sortedPayload(await readStatsPayload(db, now));
  for (const previous of ["not json", "null", JSON.stringify({ materialized_at: now - 3_600_000, metrics: [] }),
    JSON.stringify({ materialized_at: now + 60_000, rebuilt_at: now, metrics: [], snapshots: [], traffic_series: [], traffic_series_5m: [], traffic_daily: [] }),
    JSON.stringify({ materialized_at: now - 3_600_000, rebuilt_at: now - 3_600_000, metrics: [], snapshots: [], traffic_series: [], traffic_series_5m: "x", traffic_daily: [] })]) {
    const kv = memoryKv();
    kv.stored.set("stats_data", previous);
    await materializeStats(db, { KV: kv }, now);
    const { materialized_at, rebuilt_at, ...payload } = JSON.parse(kv.stored.get("stats_data")!);
    expect({ materialized_at, rebuilt_at }).toEqual({ materialized_at: now, rebuilt_at: now });
    expect(sortedPayload(payload)).toEqual(full);
  }
});

function sortedPayload(payload: StatsPayload | RequestPayload) {
  return Object.fromEntries(Object.entries(payload).map(([name, rows]) =>
    [name, Array.isArray(rows) ? rows.map((row) => JSON.stringify(row)).sort() : rows]));
}

test("the profile is recomputed once a day, not every hour", async () => {
  const kv = memoryKv();
  const T0 = Date.UTC(2026, 10, 12, 15, 0, 2);
  const first = await recordProfile(db, kv, T0);
  expect(first.as_of).toBe(new Date(T0).toISOString());
  expect((await recordProfile(db, kv, T0 + 23 * 3_600_000)).as_of).toBe(first.as_of);
  expect((await recordProfile(db, kv, T0 + 24 * 3_600_000)).as_of).toBe(new Date(T0 + 24 * 3_600_000).toISOString());
  kv.stored.set(PROFILE_KEY, "not a profile");
  expect((await recordProfile(db, kv, T0 + 25 * 3_600_000)).as_of).toBe(new Date(T0 + 25 * 3_600_000).toISOString());
});

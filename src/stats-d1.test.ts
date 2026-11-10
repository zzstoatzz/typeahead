import { afterAll, expect, test } from "bun:test";
import { rowsAs } from "./db";
import { materializeStats, writeSnapshot } from "./enrichment";
import { knownSource } from "./handlers/source-icon";
import { recordActorDelta, recordCacheHit, recordMetric, recordRequest, recordTrafficSource } from "./metrics";
import { PROFILE_KEY, parseProfile, recordProfile } from "./stats-profile";
import { STATS_TABLES, statsStores } from "./stats-d1-fixture";
import { sourceQueries, tailQueries } from "./stats-tail";

// The D1 database is created by the migrations alone, so a statement the
// statistics code issues against a table or column the migrations lack has to
// fail here rather than in production.

const { mirror: db, dispose } = await statsStores();
afterAll(dispose);

const stored = new Map<string, string>();
const kv = { async put(key: string, value: string) { stored.set(key, value); } };
const storedJson = (key: string) => JSON.parse(stored.get(key) ?? "null");

/** the fixture drops SQL comments before it applies a migration, as wrangler does */
const ddl = (sql: string) => sql.replace(/--[^\n]*/g, "").replace(/\s+/g, " ");

test("the migrations define the nine statistics tables as production does", async () => {
  const dump = ddl(await Bun.file(new URL("../docs/ops/turso-schema-20261110.sql", import.meta.url).pathname).text());
  const { results } = await db.prepare(
    "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name != 'd1_migrations' ORDER BY name",
  ).all<{ name: string; sql: string }>();
  expect(results.map((r) => r.name)).toEqual(STATS_TABLES);
  for (const table of results) expect(dump).toContain(`${ddl(table.sql)};`);
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
    await recordActorDelta(db, { actors: 3 }, now);
  }
  await recordRequest(db, "plyr.fm", "search", 503, now);
  await recordTrafficSource(db, search("smoke-testing"), now);
  await recordActorDelta(db, { actors: -1 }, now);
  await writeSnapshot(db, { hour: Math.floor(now / 3_600_000), total: 90, with_handles: 80, with_avatars: 70, hidden: 10 });
  await writeSnapshot(db, { hour: Math.floor(now / 3_600_000), total: 95, with_handles: 80, with_avatars: 70, hidden: 5 });

  expect(await db.prepare("SELECT value FROM counters WHERE name = 'actors_total'").first<{ value: number }>()).toEqual({ value: 5 });
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
}, 30_000);

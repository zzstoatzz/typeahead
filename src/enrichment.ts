import { rowsAs, type SqlDb } from "./db";
import type { RequestKvData, StatsKvData } from "./types";
import { TRAFFIC_SOURCE_WHERE } from "./metrics";

export type Snapshot = StatsKvData["snapshots"][number];

export async function writeSnapshot(db: SqlDb, snapshot: Snapshot): Promise<void> {
  await db.prepare(
    `INSERT OR REPLACE INTO snapshots (hour, total, with_handles, with_avatars, hidden)
     VALUES (?1, ?2, ?3, ?4, ?5)`
  )
    .bind(snapshot.hour, snapshot.total, snapshot.with_handles, snapshot.with_avatars, snapshot.hidden)
    .run();
}

export const SNAPSHOT_HISTORY_HOURS = 5000;

export const SNAPSHOT_HISTORY_SQL = `SELECT hour, total, with_handles, with_avatars, hidden FROM (
  SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots
  ORDER BY hour DESC LIMIT ${SNAPSHOT_HISTORY_HOURS}
) ORDER BY hour ASC`;

type StatsKv = { KV: { put(key: string, value: string): Promise<void> } };

export type StatsPayload = Omit<StatsKvData, "materialized_at">;
export type RequestPayload = Omit<RequestKvData, "materialized_at">;

/** What the cron writes to KV `stats_data`, read from the statistics store
 *  and written nowhere. */
export async function readStatsPayload(db: SqlDb, now = Date.now()): Promise<StatsPayload> {
  // metrics: 5-min buckets. 100k buckets ≈ 1 year of headroom — effectively
  // "all" for our current age. lets the /stats range selector compute true totals.
  // snapshots: hourly, the newest 5000 (~7 months), oldest first.
  // deltas: 5-min, 288 = 24h (used to stitch trend chart tail).
  // traffic: last 30d of per-domain 5-min buckets, rolled up to hourly.
  // bounded by hours × active domains (≤720 × ~dozens), NOT the corpus.
  const trafficSince = Math.floor(now / 300_000) - 30 * 288;
  const traffic5mSince = Math.floor(now / 300_000) - 2 * 288; // raw 5-min, last 48h

  const [metricsRes, snapshotRes, deltasRes, trafficRes, traffic5mRes, trafficDailyRes, sourcesRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics ORDER BY hour DESC LIMIT 100000"),
    db.prepare(SNAPSHOT_HISTORY_SQL),
    db.prepare("SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, SUM(hits) AS hits
       FROM traffic_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain ORDER BY hour ASC`
    ).bind(trafficSince),
    db.prepare(
      `SELECT bucket, domain, hits FROM traffic_buckets
       WHERE bucket >= ?1 ORDER BY bucket ASC`
    ).bind(traffic5mSince),
    // full history at daily grain: rolled-up past (traffic_daily) + the live
    // bucket window rolled up on the fly. Bounded by days × active domains,
    // NOT the corpus.
    db.prepare(
      `SELECT day, domain, SUM(hits) AS hits FROM (
         SELECT day, domain, hits FROM traffic_daily
         UNION ALL
         SELECT bucket / 288 AS day, domain, SUM(hits) AS hits
           FROM traffic_buckets GROUP BY bucket / 288, domain
       ) GROUP BY day, domain ORDER BY day ASC`
    ),
    // every app that has ever sent a search: one row per source, so a count
    // is bounded by adoption, not the corpus. The link-preview card shows it.
    db.prepare(`SELECT count(*) AS n FROM traffic_sources ${TRAFFIC_SOURCE_WHERE}
                 AND domain NOT IN ('unknown', 'homepage')`),
  ], "read");

  return {
    metrics: rowsAs<StatsPayload["metrics"][number]>(metricsRes.results ?? []),
    snapshots: rowsAs<Snapshot>(snapshotRes.results ?? []),
    deltas: rowsAs<StatsPayload["deltas"][number]>(deltasRes.results ?? []),
    traffic_series: rowsAs<NonNullable<StatsPayload["traffic_series"]>[number]>(trafficRes.results ?? []),
    traffic_series_5m: rowsAs<NonNullable<StatsPayload["traffic_series_5m"]>[number]>(traffic5mRes.results ?? []),
    traffic_daily: rowsAs<NonNullable<StatsPayload["traffic_daily"]>[number]>(trafficDailyRes.results ?? []),
    traffic_sources_total: Number(rowsAs<{ n: number }>(sourcesRes.results ?? [])[0]?.n ?? 0),
  };
}

/** materialize stats data to KV so /stats never queries the store's history on the request path */
export async function materializeStats(db: SqlDb, env: StatsKv): Promise<void> {
  // per-app history is INDEFINITE: 5-min buckets roll up into traffic_daily
  // before the 90d prune (below), so resolution decays — 5-min for 48h,
  // hourly for 30d, daily forever — but no app's history is ever dropped.
  await db.prepare(
    `CREATE TABLE IF NOT EXISTS traffic_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain)) WITHOUT ROWID`
  ).run();

  const payload = await readStatsPayload(db);
  await env.KV.put("stats_data", JSON.stringify({ ...payload, materialized_at: Date.now() } satisfies StatsKvData));

  // retention: roll buckets older than 90d into traffic_daily, THEN drop them —
  // one batch (= one transaction), so a bucket is never deleted unrolled and
  // never rolled twice. hits = hits + excluded.hits is safe for the same
  // reason: the source rows are gone the moment the transaction lands.
  const pruneBefore = Math.floor(Date.now() / 300_000) - 90 * 288;
  await db.batch([
    db.prepare(
      `INSERT INTO traffic_daily (day, domain, hits)
       SELECT bucket / 288 AS day, domain, SUM(hits) AS hits
         FROM traffic_buckets WHERE bucket < ?1
        GROUP BY bucket / 288, domain
       ON CONFLICT(day, domain) DO UPDATE SET hits = hits + excluded.hits`
    ).bind(pruneBefore),
    db.prepare("DELETE FROM traffic_buckets WHERE bucket < ?1").bind(pruneBefore),
  ]);

  await materializeRequests(db, env);
}

/** What the cron writes to KV `request_data`, read from one statistics store. */
export async function readRequestPayload(db: SqlDb, now = Date.now()): Promise<RequestPayload> {
  const nowBucket = Math.floor(now / 300_000);
  const hourlySince = nowBucket - 30 * 288;
  const fiveMinSince = nowBucket - 2 * 288;

  const [hourlyRes, fiveMinRes, dailyRes] = await db.batch([
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, endpoint, status, SUM(hits) AS hits
       FROM request_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain, endpoint, status ORDER BY hour ASC`
    ).bind(hourlySince),
    db.prepare(
      `SELECT bucket, domain, endpoint, status, hits FROM request_buckets
       WHERE bucket >= ?1 ORDER BY bucket ASC`
    ).bind(fiveMinSince),
    db.prepare(
      `SELECT day, domain, endpoint, status, SUM(hits) AS hits FROM (
         SELECT day, domain, endpoint, status, hits FROM request_daily
         UNION ALL
         SELECT bucket / 288 AS day, domain, endpoint, status, SUM(hits) AS hits
           FROM request_buckets GROUP BY bucket / 288, domain, endpoint, status
       ) GROUP BY day, domain, endpoint, status ORDER BY day ASC`
    ),
  ], "read");

  return {
    series: rowsAs<NonNullable<RequestPayload["series"]>[number]>(hourlyRes.results ?? []),
    series_5m: rowsAs<NonNullable<RequestPayload["series_5m"]>[number]>(fiveMinRes.results ?? []),
    daily: rowsAs<NonNullable<RequestPayload["daily"]>[number]>(dailyRes.results ?? []),
  };
}

/** request_buckets → KV `request_data`: every answer the worker gave, by
 *  app × endpoint × status. Same shape and retention ladder as traffic:
 *  5-min for 48h, hourly for 30d, daily forever (request_daily). Bounded by
 *  time × (active apps × endpoints × statuses), NOT the corpus — endpoint is a
 *  fixed label set (see endpointLabel) and status is an HTTP code. Its own KV
 *  key so stats_data's size is unchanged by this. */
export async function materializeRequests(db: SqlDb, env: StatsKv): Promise<void> {
  await db.prepare(
    `CREATE TABLE IF NOT EXISTS request_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL,
       status INTEGER NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain, endpoint, status)) WITHOUT ROWID`
  ).run();

  const payload = await readRequestPayload(db);
  await env.KV.put("request_data", JSON.stringify({ ...payload, materialized_at: Date.now() } satisfies RequestKvData));

  const pruneBefore = Math.floor(Date.now() / 300_000) - 90 * 288;
  await db.batch([
    db.prepare(
      `INSERT INTO request_daily (day, domain, endpoint, status, hits)
       SELECT bucket / 288 AS day, domain, endpoint, status, SUM(hits) AS hits
         FROM request_buckets WHERE bucket < ?1
        GROUP BY bucket / 288, domain, endpoint, status
       ON CONFLICT(day, domain, endpoint, status) DO UPDATE SET hits = hits + excluded.hits`
    ).bind(pruneBefore),
    db.prepare("DELETE FROM request_buckets WHERE bucket < ?1").bind(pruneBefore),
  ]);
}

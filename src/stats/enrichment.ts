import { z } from "zod";
import { rowsAs, type SqlDb } from "../db";
import type { RequestKvData, StatsKvData } from "../types";
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

/** the newest hours at or after ?1, oldest first */
export const SNAPSHOT_HISTORY_SQL = `SELECT hour, total, with_handles, with_avatars, hidden FROM (
  SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots
  WHERE hour >= ?1 ORDER BY hour DESC LIMIT ${SNAPSHOT_HISTORY_HOURS}
) ORDER BY hour ASC`;

type StatsKv = { KV: { get(key: string): Promise<string | null>; put(key: string, value: string): Promise<void> } };

export type StatsPayload = Omit<StatsKvData, "materialized_at" | "rebuilt_at">;
export type RequestPayload = Omit<RequestKvData, "materialized_at" | "rebuilt_at">;

const METRICS_LIMIT = 100_000;
const REBUILD_EVERY_MS = 24 * 3_600_000;

/** What a run keeps from the one before it. A full read scans every bucket the
 *  store holds, 90 days of them, and only the newest hour has changed. So a run
 *  keeps the rows it materialized last time and reads the store from `from`
 *  onward: the hour before the previous run's, because a count recorded as
 *  that run read can land after it. Once a day, and whenever the previous copy
 *  is missing or malformed, `from` is 0 and everything is read, so a wrong row
 *  cannot outlive a day. */
export interface Carry<T> { from: number; rebuiltAt: number; prev: T | null }

const n = z.number();
const stamps = { materialized_at: n, rebuilt_at: n };
const statsCopy = z.object({
  ...stamps,
  metrics: z.array(z.object({ hour: n, searches: n, total_ms: n, cache_hits: n, cache_ms: n })),
  snapshots: z.array(z.object({ hour: n, total: n, with_handles: n, with_avatars: n, hidden: n })),
  traffic_series: z.array(z.object({ hour: n, domain: z.string(), hits: n })),
  traffic_series_5m: z.array(z.object({ bucket: n, domain: z.string(), hits: n })),
  traffic_daily: z.array(z.object({ day: n, domain: z.string(), hits: n })),
});
const requestKey = { domain: z.string(), endpoint: z.string(), status: n, hits: n };
const requestCopy = z.object({
  ...stamps,
  series: z.array(z.object({ hour: n, ...requestKey })),
  series_5m: z.array(z.object({ bucket: n, ...requestKey })),
  daily: z.array(z.object({ day: n, ...requestKey })),
});

function carryFrom<T extends { materialized_at: number; rebuilt_at: number }>(raw: string | null, now: number, copy: z.ZodType<T>): Carry<T> {
  const full = { from: 0, rebuiltAt: now, prev: null };
  if (raw === null) return full;
  let prev: T;
  try {
    const parsed = copy.safeParse(JSON.parse(raw));
    if (!parsed.success) return full;
    prev = parsed.data;
  } catch {
    return full;
  }
  if (prev.materialized_at > now || now - prev.rebuilt_at >= REBUILD_EVERY_MS) return full;
  return { from: Math.floor(prev.materialized_at / 3_600_000) * 12 - 12, rebuiltAt: prev.rebuilt_at, prev };
}

/** `prev` rows with lo <= key < hi, then the rows read from the store */
function carried<T extends Record<K, number>, K extends string>(prev: T[] | undefined, live: T[], key: K, lo: number, hi: number): T[] {
  return [...(prev ?? []).filter((row) => row[key] >= lo && row[key] < hi), ...live];
}

/** What the cron writes to KV `stats_data`, read from the statistics store
 *  and written nowhere. */
export async function readStatsPayload(db: SqlDb, now = Date.now(), carry: Carry<z.infer<typeof statsCopy>> = { from: 0, rebuiltAt: now, prev: null }): Promise<StatsPayload> {
  // metrics: 5-min buckets. 100k buckets ≈ 1 year of headroom — effectively
  // "all" for our current age. lets the /stats range selector compute true totals.
  // snapshots: hourly, the newest 5000 (~7 months), oldest first.
  // deltas: 5-min, 288 = 24h (used to stitch trend chart tail).
  // traffic: last 30d of per-domain 5-min buckets, rolled up to hourly.
  // bounded by hours × active domains (≤720 × ~dozens), NOT the corpus.
  const { from, prev } = carry;
  const nowBucket = Math.floor(now / 300_000);
  // whole hours, so an hour carried from an earlier run is the hour a full read returns
  const hourlySince = Math.floor(nowBucket / 12) * 12 - 30 * 288;
  const fiveMinSince = nowBucket - 2 * 288; // raw 5-min, last 48h
  const fromDay = Math.floor(from / 288);

  const [metricsRes, snapshotRes, deltasRes, trafficRes, traffic5mRes, trafficDailyRes, sourcesRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics WHERE hour >= ?1 ORDER BY hour DESC LIMIT ?2").bind(from, METRICS_LIMIT),
    db.prepare(SNAPSHOT_HISTORY_SQL).bind(Math.floor(from / 12)),
    db.prepare("SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, SUM(hits) AS hits
       FROM traffic_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain ORDER BY hour ASC`
    ).bind(Math.max(hourlySince, from)),
    db.prepare(
      `SELECT bucket, domain, hits FROM traffic_buckets
       WHERE bucket >= ?1 ORDER BY bucket ASC`
    ).bind(Math.max(fiveMinSince, from)),
    // full history at daily grain: rolled-up past (traffic_daily) + the live
    // bucket window rolled up on the fly. Bounded by days × active domains,
    // NOT the corpus.
    db.prepare(
      `SELECT day, domain, SUM(hits) AS hits FROM (
         SELECT day, domain, hits FROM traffic_daily WHERE day >= ?1
         UNION ALL
         SELECT bucket / 288 AS day, domain, SUM(hits) AS hits
           FROM traffic_buckets WHERE bucket >= ?1 * 288 GROUP BY bucket / 288, domain
       ) GROUP BY day, domain ORDER BY day ASC`
    ).bind(fromDay),
    // every app that has ever sent a search: one row per source, so a count
    // is bounded by adoption, not the corpus. The link-preview card shows it.
    db.prepare(`SELECT count(*) AS n FROM traffic_sources ${TRAFFIC_SOURCE_WHERE}
                 AND domain NOT IN ('unknown', 'homepage')`),
  ], "read");

  const metrics = rowsAs<StatsPayload["metrics"][number]>(metricsRes.results ?? []);
  return {
    // newest first, unlike the rest
    metrics: [...metrics, ...(prev?.metrics ?? []).filter((row) => row.hour < from)].slice(0, METRICS_LIMIT),
    snapshots: carried(prev?.snapshots, rowsAs<Snapshot>(snapshotRes.results ?? []), "hour", 0, Math.floor(from / 12)).slice(-SNAPSHOT_HISTORY_HOURS),
    deltas: rowsAs<StatsPayload["deltas"][number]>(deltasRes.results ?? []),
    traffic_series: carried(prev?.traffic_series, rowsAs<NonNullable<StatsPayload["traffic_series"]>[number]>(trafficRes.results ?? []), "hour", hourlySince / 12, from / 12),
    traffic_series_5m: carried(prev?.traffic_series_5m, rowsAs<NonNullable<StatsPayload["traffic_series_5m"]>[number]>(traffic5mRes.results ?? []), "bucket", fiveMinSince, from),
    traffic_daily: carried(prev?.traffic_daily, rowsAs<NonNullable<StatsPayload["traffic_daily"]>[number]>(trafficDailyRes.results ?? []), "day", 0, fromDay),
    traffic_sources_total: Number(rowsAs<{ n: number }>(sourcesRes.results ?? [])[0]?.n ?? 0),
  };
}

/** materialize stats data to KV so /stats never queries the store's history on the request path */
export async function materializeStats(db: SqlDb, env: StatsKv, now = Date.now()): Promise<void> {
  // per-app history is INDEFINITE: 5-min buckets roll up into traffic_daily
  // before the 90d prune (below), so resolution decays — 5-min for 48h,
  // hourly for 30d, daily forever — but no app's history is ever dropped.
  await db.prepare(
    `CREATE TABLE IF NOT EXISTS traffic_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain)) WITHOUT ROWID`
  ).run();

  const carry = carryFrom(await env.KV.get("stats_data"), now, statsCopy);
  const payload = await readStatsPayload(db, now, carry);
  // stamped with the instant the read was for: the next run and the page both
  // resume from its hour
  await env.KV.put("stats_data", JSON.stringify({ ...payload, materialized_at: now, rebuilt_at: carry.rebuiltAt } satisfies StatsKvData));

  // retention: roll buckets older than 90d into traffic_daily, THEN drop them —
  // one batch (= one transaction), so a bucket is never deleted unrolled and
  // never rolled twice. hits = hits + excluded.hits is safe for the same
  // reason: the source rows are gone the moment the transaction lands.
  const pruneBefore = Math.floor(now / 300_000) - 90 * 288;
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

  await materializeRequests(db, env, now);
}

/** What the cron writes to KV `request_data`, read from one statistics store. */
export async function readRequestPayload(db: SqlDb, now = Date.now(), carry: Carry<z.infer<typeof requestCopy>> = { from: 0, rebuiltAt: now, prev: null }): Promise<RequestPayload> {
  const { from, prev } = carry;
  const nowBucket = Math.floor(now / 300_000);
  const hourlySince = Math.floor(nowBucket / 12) * 12 - 30 * 288;
  const fiveMinSince = nowBucket - 2 * 288;
  const fromDay = Math.floor(from / 288);

  const [hourlyRes, fiveMinRes, dailyRes] = await db.batch([
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, endpoint, status, SUM(hits) AS hits
       FROM request_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain, endpoint, status ORDER BY hour ASC`
    ).bind(Math.max(hourlySince, from)),
    db.prepare(
      `SELECT bucket, domain, endpoint, status, hits FROM request_buckets
       WHERE bucket >= ?1 ORDER BY bucket ASC`
    ).bind(Math.max(fiveMinSince, from)),
    db.prepare(
      `SELECT day, domain, endpoint, status, SUM(hits) AS hits FROM (
         SELECT day, domain, endpoint, status, hits FROM request_daily WHERE day >= ?1
         UNION ALL
         SELECT bucket / 288 AS day, domain, endpoint, status, SUM(hits) AS hits
           FROM request_buckets WHERE bucket >= ?1 * 288 GROUP BY bucket / 288, domain, endpoint, status
       ) GROUP BY day, domain, endpoint, status ORDER BY day ASC`
    ).bind(fromDay),
  ], "read");

  return {
    series: carried(prev?.series, rowsAs<NonNullable<RequestPayload["series"]>[number]>(hourlyRes.results ?? []), "hour", hourlySince / 12, from / 12),
    series_5m: carried(prev?.series_5m, rowsAs<NonNullable<RequestPayload["series_5m"]>[number]>(fiveMinRes.results ?? []), "bucket", fiveMinSince, from),
    daily: carried(prev?.daily, rowsAs<NonNullable<RequestPayload["daily"]>[number]>(dailyRes.results ?? []), "day", 0, fromDay),
  };
}

/** request_buckets → KV `request_data`: every answer the worker gave, by
 *  app × endpoint × status. Same shape and retention ladder as traffic:
 *  5-min for 48h, hourly for 30d, daily forever (request_daily). Bounded by
 *  time × (active apps × endpoints × statuses), NOT the corpus — endpoint is a
 *  fixed label set (see endpointLabel) and status is an HTTP code. Its own KV
 *  key so stats_data's size is unchanged by this. */
export async function materializeRequests(db: SqlDb, env: StatsKv, now = Date.now()): Promise<void> {
  await db.prepare(
    `CREATE TABLE IF NOT EXISTS request_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL,
       status INTEGER NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain, endpoint, status)) WITHOUT ROWID`
  ).run();

  const carry = carryFrom(await env.KV.get("request_data"), now, requestCopy);
  const payload = await readRequestPayload(db, now, carry);
  await env.KV.put("request_data", JSON.stringify({ ...payload, materialized_at: now, rebuilt_at: carry.rebuiltAt } satisfies RequestKvData));

  const pruneBefore = Math.floor(now / 300_000) - 90 * 288;
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

import type { Stmt, SqlDb } from "./db";

/** The cron writes /stats data to KV once an hour, so its newest buckets are up
 *  to an hour old. The page reads every bucket from the top of the hour the KV
 *  copy was written onward straight from Turso and swaps it in: at most two
 *  hours of 5-minute buckets, each read a primary-key range on `bucket`/`hour`,
 *  so a short window such as the last hour is current rather than the hour
 *  before. Starting on an hour boundary lets the hourly series swap whole
 *  hours without double counting a partial one. */
export function tailStartBucket(materializedAt: number | undefined, now = Date.now()): number {
  const anchor = Math.min(materializedAt ?? now - 3_600_000, now);
  return Math.floor(anchor / 3_600_000) * 12;
}

/** rows at or after `start` (by `key`) come from `live`; older rows stay */
export function withTail<T extends Record<K, number>, K extends string>(kv: T[], live: T[], key: K, start: number): T[] {
  return [...kv.filter((r) => r[key] < start), ...live];
}

export function tailQueries(db: SqlDb, trafficStart: number, requestStart: number): Stmt[] {
  return [
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics WHERE hour >= ?1 ORDER BY hour DESC").bind(trafficStart),
    db.prepare("SELECT bucket, domain, hits FROM traffic_buckets WHERE bucket >= ?1 ORDER BY bucket ASC").bind(trafficStart),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, SUM(hits) AS hits FROM traffic_buckets
       WHERE bucket >= ?1 GROUP BY bucket / 12, domain ORDER BY hour ASC`,
    ).bind(trafficStart),
    db.prepare("SELECT bucket, domain, endpoint, status, hits FROM request_buckets WHERE bucket >= ?1 ORDER BY bucket ASC").bind(requestStart),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, endpoint, status, SUM(hits) AS hits FROM request_buckets
       WHERE bucket >= ?1 GROUP BY bucket / 12, domain, endpoint, status ORDER BY hour ASC`,
    ).bind(requestStart),
  ];
}

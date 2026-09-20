import type { TursoDB } from "./db";
import type { Env } from "./types";
import { TRAFFIC_SOURCE_WHERE } from "./metrics";

/** record an actor-count snapshot for the current hour (idempotent).
 *
 *  the corpus is ~6M rows; a `COUNT(*)` / `SUM(CASE …)` over the whole table
 *  takes ~300s over libSQL HTTP, which silently blew the cron's budget once the
 *  table grew (snapshots froze 2026-05-18). instead:
 *    - `total`        — O(1) read of the maintained `actors_total` counter
 *    - `hidden`       — selective count via `idx_actors_hidden` (partial index)
 *    - empty handles  — selective range seek on `idx_actors_handle`
 *    - empty avatars  — selective count via `idx_actors_avatar_empty`
 *  with_handles / with_avatars are then `total - empty`. every count touches
 *  only a small slice (<400k matching rows), so the whole thing is sub-second. */
export async function recordSnapshot(db: TursoDB): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  const [totalRes, hiddenRes, emptyHandlesRes, emptyAvatarsRes] = await db.batch([
    db.prepare("SELECT value AS n FROM counters WHERE name = 'actors_total'"),
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE hidden != 0"),
    // COLLATE NOCASE or this is a SCAN of all ~10.6M index entries instead of a
    // SEARCH: idx_actors_handle is declared COLLATE NOCASE, so a binary-equality
    // predicate cannot seek it and falls back to idx_actors_enrich_identity as a
    // covering scan. Cheap when idle, but it is the most expensive query in the
    // hourly cron and the first to time out under write contention — which
    // fails the step and leaves that hour with no snapshot row.
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE handle = '' COLLATE NOCASE"),
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE avatar_url = ''"),
  ], "read");

  const num = (r: any): number => Number(r?.results?.[0]?.n ?? 0);
  const total = num(totalRes);
  if (total <= 0) return; // counter not seeded yet — don't write a bogus zero row

  const hidden = num(hiddenRes);
  const withHandles = total - num(emptyHandlesRes);
  const withAvatars = total - num(emptyAvatarsRes);

  await db.prepare(
    `INSERT OR REPLACE INTO snapshots (hour, total, with_handles, with_avatars, hidden)
     VALUES (?1, ?2, ?3, ?4, ?5)`
  )
    .bind(hour, total - hidden, withHandles, withAvatars, hidden)
    .run();
}

/** materialize stats data to KV so /stats never queries turso on the request path */
export async function materializeStats(db: TursoDB, env: Env): Promise<void> {
  // metrics: 5-min buckets. 100k buckets ≈ 1 year of headroom — effectively
  // "all" for our current age. lets the /stats range selector compute true totals.
  // snapshots: hourly. 5000 covers ~7 months.
  // deltas: 5-min, 288 = 24h (used to stitch trend chart tail).
  // traffic: last 30d of per-domain 5-min buckets, rolled up to hourly.
  // bounded by hours × active domains (≤720 × ~dozens), NOT the corpus.
  const trafficSince = Math.floor(Date.now() / 300_000) - 30 * 288;
  const traffic5mSince = Math.floor(Date.now() / 300_000) - 2 * 288; // raw 5-min, last 48h

  // per-app history is INDEFINITE: 5-min buckets roll up into traffic_daily
  // before the 90d prune (below), so resolution decays — 5-min for 48h,
  // hourly for 30d, daily forever — but no app's history is ever dropped.
  await db.prepare(
    `CREATE TABLE IF NOT EXISTS traffic_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain)) WITHOUT ROWID`
  ).run();

  const [metricsRes, snapshotRes, deltasRes, trafficRes, traffic5mRes, trafficDailyRes, sourcesRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics ORDER BY hour DESC LIMIT 100000"),
    db.prepare("SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots ORDER BY hour ASC LIMIT 5000"),
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

  await env.KV.put("stats_data", JSON.stringify({
    metrics: metricsRes.results ?? [],
    snapshots: snapshotRes.results ?? [],
    deltas: deltasRes.results ?? [],
    traffic_series: trafficRes.results ?? [],
    traffic_series_5m: traffic5mRes.results ?? [],
    traffic_daily: trafficDailyRes.results ?? [],
    traffic_sources_total: Number((sourcesRes.results?.[0] as { n?: number } | undefined)?.n ?? 0),
    materialized_at: Date.now(),
  }));

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

/** request_buckets → KV `request_data`: every answer the worker gave, by
 *  app × endpoint × status. Same shape and retention ladder as traffic:
 *  5-min for 48h, hourly for 30d, daily forever (request_daily). Bounded by
 *  time × (active apps × endpoints × statuses), NOT the corpus — endpoint is a
 *  fixed label set (see endpointLabel) and status is an HTTP code. Its own KV
 *  key so stats_data's size is unchanged by this. */
export async function materializeRequests(db: TursoDB, env: Env): Promise<void> {
  const nowBucket = Math.floor(Date.now() / 300_000);
  const hourlySince = nowBucket - 30 * 288;
  const fiveMinSince = nowBucket - 2 * 288;

  await db.prepare(
    `CREATE TABLE IF NOT EXISTS request_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL,
       status INTEGER NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain, endpoint, status)) WITHOUT ROWID`
  ).run();

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

  await env.KV.put("request_data", JSON.stringify({
    series: hourlyRes.results ?? [],
    series_5m: fiveMinRes.results ?? [],
    daily: dailyRes.results ?? [],
    materialized_at: Date.now(),
  }));

  const pruneBefore = nowBucket - 90 * 288;
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

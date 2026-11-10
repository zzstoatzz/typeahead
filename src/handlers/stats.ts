import { rowsAs, type SqlDb } from "../db";
import type { Env, RequestKvData, StatsKvData } from "../types";
import { html } from "../utils";
import { statsPage, type SnapshotPoint } from "../pages/stats";
import { EXCLUSIONS_KEY, parseExclusions } from "../exclusions";
import { sourceQueries, tailQueries, tailStartBucket, withTail } from "../stats-tail";
import { PROFILE_KEY, parseProfile } from "../stats-profile";

const EDGE_CACHE_TTL = 60; // seconds

export async function handleStats(request: Request, db: SqlDb, env: Env, ctx: ExecutionContext): Promise<Response> {
  const cache = caches.default;
  // The key used to be a bare "/stats", discarding the query — so every shared
  // comparison URL collapsed onto one cache entry and a crawler unfurling
  // ?apps=a,b got whatever generic page happened to be cached. Normalise the
  // two params that change the render and key on those, so unknown junk
  // (?utm_source=…) still shares an entry instead of fragmenting the cache.
  const reqUrl = new URL(request.url);
  const apps = (reqUrl.searchParams.get("apps") ?? "")
    .split(",").map((s) => s.trim()).filter(Boolean).slice(0, 8);
  const range = ["1h", "24h", "7d", "30d", "all"].includes(reqUrl.searchParams.get("range") ?? "")
    ? reqUrl.searchParams.get("range")! : "7d";
  const norm = new URL("/stats", request.url);
  norm.searchParams.set("metric", "expected-range-v7");
  if (apps.length) norm.searchParams.set("apps", apps.join(","));
  if (range !== "7d") norm.searchParams.set("range", range);
  const cacheKey = new Request(norm.href);
  const cached = await cache.match(cacheKey);
  if (cached) {
    const resp = new Response(cached.body, cached);
    resp.headers.set("Cache-Control", "no-store");
    return resp;
  }

  const t0 = performance.now();

  // materialized data from KV (written by cron) — never hits turso
  const [kvData, reqData, exclusions, profile] = await Promise.all([
    env.KV.get<StatsKvData>("stats_data", "json"),
    env.KV.get<RequestKvData>("request_data", "json"),
    env.KV.get(EXCLUSIONS_KEY).then(parseExclusions),
    env.KV.get(PROFILE_KEY).then(parseProfile),
  ]);

  const trafficStart = tailStartBucket(kvData?.materialized_at);
  const requestStart = tailStartBucket(reqData?.materialized_at);
  const [trafficRes, trafficAllRes, metricsTail, traffic5mTail, trafficHourTail, request5mTail, requestHourTail] = await db.batch([
    ...sourceQueries(db),
    ...tailQueries(db, trafficStart, requestStart),
  ], "read");
  type Metric = StatsKvData["metrics"][number];
  type Traffic5m = NonNullable<StatsKvData["traffic_series_5m"]>[number];
  type TrafficHour = NonNullable<StatsKvData["traffic_series"]>[number];
  type Request5m = NonNullable<RequestKvData["series_5m"]>[number];
  type RequestHour = NonNullable<RequestKvData["series"]>[number];

  const tQuery = performance.now();

  // metrics are newest-first; the series are oldest-first
  const rows = [...rowsAs<Metric>(metricsTail?.results ?? []), ...(kvData?.metrics ?? []).filter((r) => r.hour < trafficStart)];
  const dbSnapshots = kvData?.snapshots ?? [];
  const trafficSources = rowsAs<{ domain: string; hits: number }>(trafficRes.results);
  const trafficAll = rowsAs<{ domains: number; hits: number }>(trafficAllRes.results)[0] ?? { domains: 0, hits: 0 };
  const deltas = [...(kvData?.deltas ?? [])].reverse();

  // build snapshot points with timestamps
  const snapshots: SnapshotPoint[] = dbSnapshots.map((s) => ({
    ts: s.hour * 3_600_000,
    total: s.total,
    with_handles: s.with_handles,
    with_avatars: s.with_avatars,
  }));

  // stitch deltas after last snapshot for interpolated points
  if (snapshots.length > 0 && deltas.length > 0) {
    const lastSnap = snapshots[snapshots.length - 1];
    const lastSnapBucket = Math.floor(lastSnap.ts / 300_000);
    let cumActors = 0, cumHandles = 0, cumAvatars = 0;
    for (const d of deltas) {
      if (d.bucket <= lastSnapBucket) continue;
      cumActors += d.actors_delta;
      cumHandles += d.handles_delta;
      cumAvatars += d.avatars_delta;
      snapshots.push({
        ts: d.bucket * 300_000,
        total: lastSnap.total + cumActors,
        with_handles: lastSnap.with_handles + cumHandles,
        with_avatars: lastSnap.with_avatars + cumAvatars,
      });
    }
  }

  const latest = snapshots[snapshots.length - 1] ?? { total: 0, with_handles: 0, with_avatars: 0 };
  const total = latest.total;
  const withHandles = latest.with_handles;
  const withAvatars = latest.with_avatars;

  const totalSearches = rows.reduce((s, r) => s + r.searches + (r.cache_hits ?? 0), 0);
  // avg latency over last 24h only (288 five-min buckets) so transient spikes age out quickly
  const recentRows = rows.slice(0, 288);
  const recentSearches = recentRows.reduce((s, r) => s + r.searches, 0);
  const recentMs = recentRows.reduce((s, r) => s + r.total_ms, 0);
  const avgLatency = recentSearches > 0 ? recentMs / recentSearches : 0;
  const recentCacheHits = recentRows.reduce((s, r) => s + (r.cache_hits ?? 0), 0);
  const recentCacheMs = recentRows.reduce((s, r) => s + (r.cache_ms ?? 0), 0);
  const avgCacheLatency = recentCacheHits > 0 ? recentCacheMs / recentCacheHits : 0;
  const handlePct = total > 0 ? ((withHandles / total) * 100).toFixed(1) : "0";
  const avatarPct = total > 0 ? ((withAvatars / total) * 100).toFixed(1) : "0";

  const tProcess = performance.now();

  const body = statsPage({ total, exclusions, profile, rows, totalSearches, avgLatency, avgCacheLatency, handlePct, avatarPct, snapshots, trafficSources, trafficAll, trafficSeries: withTail(kvData?.traffic_series ?? [], rowsAs<TrafficHour>(trafficHourTail?.results ?? []), "hour", trafficStart / 12),
    trafficSeries5m: withTail(kvData?.traffic_series_5m ?? [], rowsAs<Traffic5m>(traffic5mTail?.results ?? []), "bucket", trafficStart),
    trafficDaily: kvData?.traffic_daily ?? [],
    requestSeries: withTail(reqData?.series ?? [], rowsAs<RequestHour>(requestHourTail?.results ?? []), "hour", requestStart / 12),
    requestSeries5m: withTail(reqData?.series_5m ?? [], rowsAs<Request5m>(request5mTail?.results ?? []), "bucket", requestStart),
    requestDaily: reqData?.daily ?? [],
    view: { apps, range, origin: reqUrl.origin } });

  const tRender = performance.now();

  const queryMs = (tQuery - t0).toFixed(0);
  const processMs = (tProcess - tQuery).toFixed(0);
  const renderMs = (tRender - tProcess).toFixed(0);
  const totalMs2 = (tRender - t0).toFixed(0);

  const timing = `query;dur=${queryMs}, process;dur=${processMs}, render;dur=${renderMs}, total;dur=${totalMs2}`;

  const cacheResponse = html(body, {
    "Server-Timing": timing,
    "Cache-Control": `public, max-age=${EDGE_CACHE_TTL}`,
  });
  ctx.waitUntil(cache.put(cacheKey, cacheResponse));

  return html(body, {
    "Server-Timing": timing,
    "Cache-Control": "no-store",
  });
}

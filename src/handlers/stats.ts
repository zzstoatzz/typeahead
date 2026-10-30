import { rowsAs, type TursoDB } from "../db";
import type { Env, RequestKvData, StatsKvData } from "../types";
import { html } from "../utils";
import { TRAFFIC_SOURCE_WHERE } from "../metrics";
import { statsPage, type SnapshotPoint } from "../pages/stats";

const EDGE_CACHE_TTL = 60; // seconds

export async function handleStats(request: Request, db: TursoDB, env: Env, ctx: ExecutionContext): Promise<Response> {
  const cache = caches.default;
  // The key used to be a bare "/stats", discarding the query — so every shared
  // comparison URL collapsed onto one cache entry and a crawler unfurling
  // ?apps=a,b got whatever generic page happened to be cached. Normalise the
  // two params that change the render and key on those, so unknown junk
  // (?utm_source=…) still shares an entry instead of fragmenting the cache.
  const reqUrl = new URL(request.url);
  const apps = (reqUrl.searchParams.get("apps") ?? "")
    .split(",").map((s) => s.trim()).filter(Boolean).slice(0, 8);
  const range = ["24h", "7d", "30d", "all"].includes(reqUrl.searchParams.get("range") ?? "")
    ? reqUrl.searchParams.get("range")! : "7d";
  const norm = new URL("/stats", request.url);
  norm.searchParams.set("metric", "search-exclusions-v1");
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
  const [kvData, reqData, exclusions] = await Promise.all([
    env.KV.get<StatsKvData>("stats_data", "json"),
    env.KV.get<RequestKvData>("request_data", "json"),
    env.KV.get<{ count: number; as_of: string }>("search_exclusions", "json"),
  ]);

  const trafficWhere = TRAFFIC_SOURCE_WHERE;
  // the ranked rows the page lists, and the whole table's total so the pie
  // can draw what the rows leave out. traffic_sources is one row per source
  // (219 on 2026-09-19), so neither query is corpus-proportional. The list
  // was LIMIT 50 until 2026-09-19, which silently dropped every source past
  // rank 50 from the page; the page now folds the tail behind a button
  // instead, and the LIMIT is only a ceiling on page size.
  const [trafficRes, trafficAllRes] = await db.batch([
    db.prepare(`SELECT domain, hits FROM traffic_sources ${trafficWhere} ORDER BY hits DESC LIMIT 1000`),
    db.prepare(`SELECT count(*) AS domains, coalesce(sum(hits), 0) AS hits FROM traffic_sources ${trafficWhere}`),
  ], "read");

  const tQuery = performance.now();

  const rows = kvData?.metrics ?? [];
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
  const hiddenCount = exclusions?.count ?? null;

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

  const body = statsPage({ total, hiddenCount, exclusionsAsOf: exclusions?.as_of, rows, totalSearches, avgLatency, avgCacheLatency, handlePct, avatarPct, snapshots, trafficSources, trafficAll, trafficSeries: kvData?.traffic_series ?? [], trafficSeries5m: kvData?.traffic_series_5m ?? [], trafficDaily: kvData?.traffic_daily ?? [],
    requestSeries: reqData?.series ?? [], requestSeries5m: reqData?.series_5m ?? [], requestDaily: reqData?.daily ?? [],
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

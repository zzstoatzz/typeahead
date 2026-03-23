import type { TursoDB } from "../db";
import type { Env } from "../types";
import { html } from "../utils";
import { statsPage, type SnapshotPoint } from "../pages/stats";

const EDGE_CACHE_TTL = 60; // seconds

export async function handleStats(request: Request, db: TursoDB, env: Env, ctx: ExecutionContext): Promise<Response> {
  const cache = caches.default;
  const cacheKey = new Request(new URL("/stats", request.url).href);
  const cached = await cache.match(cacheKey);
  if (cached) {
    const resp = new Response(cached.body, cached);
    resp.headers.set("Cache-Control", "no-store");
    return resp;
  }

  const t0 = performance.now();

  // materialized data from KV (written by cron) — never hits turso
  const kvData = await env.KV.get("stats_data", "json") as {
    metrics: { hour: number; searches: number; total_ms: number }[];
    snapshots: { hour: number; total: number; with_handles: number; with_avatars: number; hidden: number }[];
    deltas: { bucket: number; actors_delta: number; handles_delta: number; avatars_delta: number }[];
    materialized_at: number;
  } | null;

  // traffic sources: live from turso (7 rows, real-time)
  const trafficRes = await db.prepare(
    "SELECT domain, hits FROM traffic_sources ORDER BY hits DESC LIMIT 10"
  ).all<{ domain: string; hits: number }>();

  const tQuery = performance.now();

  const rows = kvData?.metrics ?? [];
  const dbSnapshots = kvData?.snapshots ?? [];
  const trafficSources = trafficRes.results ?? [];
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
  const hiddenCount = dbSnapshots.length > 0 ? (dbSnapshots[dbSnapshots.length - 1].hidden ?? 0) : 0;

  const totalSearches = rows.reduce((s, r) => s + r.searches, 0);
  const totalMs = rows.reduce((s, r) => s + r.total_ms, 0);
  const avgLatency = totalSearches > 0 ? totalMs / totalSearches : 0;
  const handlePct = total > 0 ? ((withHandles / total) * 100).toFixed(1) : "0";
  const avatarPct = total > 0 ? ((withAvatars / total) * 100).toFixed(1) : "0";

  const tProcess = performance.now();

  const body = statsPage({ total, hiddenCount, rows, totalSearches, avgLatency, handlePct, avatarPct, snapshots, trafficSources });

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

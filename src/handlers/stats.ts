import type { TursoDB } from "../db";
import { html } from "../utils";
import { statsPage, type SnapshotPoint } from "../pages/stats";

const EDGE_CACHE_TTL = 60; // seconds — stats are backward-looking, 1 min staleness is invisible

export async function handleStats(request: Request, db: TursoDB, ctx: ExecutionContext): Promise<Response> {
  // serve from CF edge cache when available (avoids Turso cold-start penalty)
  const cache = caches.default;
  const cacheKey = new Request(new URL("/stats", request.url).href);
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const t0 = performance.now();

  const [metricsRes, snapshotRes, trafficRes, deltasRes] =
    await db.batch([
      db.prepare(
        "SELECT hour, searches, total_ms FROM metrics ORDER BY hour DESC LIMIT 2016"
      ),
      db.prepare(
        "SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots ORDER BY hour ASC LIMIT 2000"
      ),
      db.prepare(
        "SELECT domain, hits FROM traffic_sources ORDER BY hits DESC LIMIT 10"
      ),
      db.prepare(
        "SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"
      ),
    ], "read");

  const tQuery = performance.now();

  const rows = (metricsRes.results ?? []) as {
    hour: number;
    searches: number;
    total_ms: number;
  }[];
  const dbSnapshots = (snapshotRes.results ?? []) as { hour: number; total: number; with_handles: number; with_avatars: number; hidden: number }[];
  const trafficSources = (trafficRes.results ?? []) as { domain: string; hits: number }[];
  const deltas = ((deltasRes.results ?? []) as { bucket: number; actors_delta: number; handles_delta: number; avatars_delta: number }[]).reverse();

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

  // derive current counts from the latest point (snapshot + deltas)
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

  const response = html(body, {
    "Server-Timing": `query;dur=${queryMs}, process;dur=${processMs}, render;dur=${renderMs}, total;dur=${totalMs2}`,
    "Cache-Control": `public, max-age=${EDGE_CACHE_TTL}`,
  });

  // populate edge cache — use waitUntil so it completes after response is sent
  ctx.waitUntil(cache.put(cacheKey, response.clone()));

  return response;
}

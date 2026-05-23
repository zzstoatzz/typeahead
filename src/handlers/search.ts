import type { TursoDB } from "../db";
import type { ActorRow, Env } from "../types";
import { CORS_HEADERS, BSKY_TYPEAHEAD_URL } from "../types";
import { json, sanitize, avatarUrl } from "../utils";
import { recordMetric, recordCacheHit, recordTrafficSource } from "../metrics";
import { throttledBackfill } from "../backfill";

export async function handleSearch(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const q = url.searchParams.get("q") || url.searchParams.get("term") || "";
  const limitRaw = url.searchParams.get("limit");
  const limitParam = limitRaw !== null ? parseInt(limitRaw, 10) : 10;

  if (!q.trim()) {
    return json({ error: "InvalidRequest", message: "Error: Params must have the property \"q\"" }, 400);
  }
  if (isNaN(limitParam) || limitParam < 1 || limitParam > 100) {
    return json({ error: "InvalidRequest", message: "Error: limit must be between 1 and 100" }, 400);
  }

  const limit = limitParam;
  const term = sanitize(q);
  if (!term) {
    return json({ actors: [] });
  }

  // Emergency kill switch: bypass the whole pipeline and proxy to Bluesky.
  // Set/unset via `bunx wrangler secret put|delete EMERGENCY_PROXY_BSKY`.
  // No cache writes here — old cache entries expire (60s) and the moment the
  // switch is flipped off we resume normal behavior.
  if (env.EMERGENCY_PROXY_BSKY) {
    const bskyUrl = `${BSKY_TYPEAHEAD_URL}?q=${encodeURIComponent(term)}&limit=${limit}`;
    try {
      const r = await fetch(bskyUrl, { signal: AbortSignal.timeout(5000) });
      const body = await r.text();
      return new Response(body, {
        status: r.status,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      });
    } catch {
      return json({ actors: [] });
    }
  }

  const t0 = Date.now();

  // cache API — edge cache for hot queries
  // hostname must belong to this zone so the CF zone-purge API can target it
  const host = new URL(request.url).host;
  const cacheKey = new Request(
    `https://${host}/__cache/${encodeURIComponent(term)}:${limit}`,
    request
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    ctx.waitUntil(recordCacheHit(db, Date.now() - t0));
    return cached;
  }

  // try fly backend (local SQLite replica) if configured
  if (env.SEARCH_BACKEND_URL) {
    try {
      const backendUrl = `${env.SEARCH_BACKEND_URL}/search?q=${encodeURIComponent(term)}&limit=${limit}`;
      const backendRes = await fetch(backendUrl, { signal: AbortSignal.timeout(3000) });
      if (backendRes.ok) {
        const body = await backendRes.text();

        ctx.waitUntil(Promise.all([
          recordMetric(db, Date.now() - t0),
          recordTrafficSource(db, request),
        ]));

        const response = new Response(body, {
          headers: { "Content-Type": "application/json", ...CORS_HEADERS },
        });

        const cacheable = new Response(response.clone().body, response);
        cacheable.headers.set("Cache-Control", "public, max-age=60");
        ctx.waitUntil(cache.put(cacheKey, cacheable));

        return response;
      }
    } catch {
      // fall through to turso
    }
  }

  // fallback: 3-tier ranking via turso
  const ftsQuery = `"${term}"*`;
  const [exactRes, prefixRes, ftsRes] = await db.batch([
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
       FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1`
    ).bind(term),
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
       FROM actors WHERE handle LIKE ?1 COLLATE NOCASE AND handle != ?2 COLLATE NOCASE AND hidden = 0
       LIMIT ?3`
    ).bind(term + '%', term, limit),
    db.prepare(
      `SELECT a.did, a.handle, a.display_name, a.avatar_url, a.labels, a.created_at, a.associated, a.pds
       FROM actors_fts
       JOIN actors a ON a.rowid = actors_fts.rowid
       WHERE actors_fts MATCH ?1 AND a.handle != '' AND a.hidden = 0
       ORDER BY rank
       LIMIT ?2`
    ).bind(ftsQuery, limit),
  ], "read");

  const seen = new Set<string>();
  const merged: ActorRow[] = [];
  for (const row of [
    ...((exactRes.results as ActorRow[]) || []),
    ...((prefixRes.results as ActorRow[]) || []),
    ...((ftsRes.results as ActorRow[]) || []),
  ]) {
    if (seen.has(row.did)) continue;
    seen.add(row.did);
    merged.push(row);
    if (merged.length >= limit) break;
  }

  const actors = merged.map((r) => ({
    did: r.did,
    handle: r.handle,
    ...(r.display_name ? { displayName: r.display_name } : {}),
    ...(r.avatar_url ? { avatar: avatarUrl(r.did, r.avatar_url, r.pds) } : {}),
    ...(r.associated && r.associated !== '{}' ? { associated: JSON.parse(r.associated) } : {}),
    labels: JSON.parse(r.labels || '[]'),
    ...(r.created_at ? { createdAt: r.created_at } : {}),
  }));

  // --- backfill: remove this block once at parity with Bluesky ---
  const hasGaps = actors.length < limit || actors.some((a) => !a.avatar);
  if (hasGaps) {
    ctx.waitUntil(throttledBackfill(term, limit, db, env));
  }
  // --- end backfill ---

  ctx.waitUntil(Promise.all([
    recordMetric(db, Date.now() - t0),
    recordTrafficSource(db, request),
  ]));

  const response = json({ actors });

  // cache for 60 seconds — non-blocking so it doesn't delay the response
  const cacheable = new Response(response.clone().body, response);
  cacheable.headers.set("Cache-Control", "public, max-age=60");
  ctx.waitUntil(cache.put(cacheKey, cacheable));

  return response;
}

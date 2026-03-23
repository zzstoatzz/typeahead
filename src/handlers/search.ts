import type { TursoDB } from "../db";
import type { ActorRow, Env } from "../types";
import { json, sanitize, avatarUrl } from "../utils";
import { recordMetric, recordTrafficSource } from "../metrics";
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

  // cache API — edge cache for hot queries
  const cacheKey = new Request(
    `https://typeahead-cache/${encodeURIComponent(term)}:${limit}`,
    request
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }

  const t0 = Date.now();

  // 3-tier ranking: exact handle → handle prefix → FTS prefix
  const ftsQuery = `"${term}"*`;
  const [exactRes, prefixRes, ftsRes] = await db.batch([
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated
       FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1`
    ).bind(term),
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated
       FROM actors WHERE handle LIKE ?1 COLLATE NOCASE AND handle != ?2 COLLATE NOCASE AND hidden = 0
       ORDER BY length(handle) LIMIT ?3`
    ).bind(term + '%', term, limit),
    db.prepare(
      `SELECT a.did, a.handle, a.display_name, a.avatar_url, a.labels, a.created_at, a.associated
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
    ...(r.avatar_url ? { avatar: avatarUrl(r.did, r.avatar_url) } : {}),
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

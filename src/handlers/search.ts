import type { TursoDB } from "../db";
import type { ActorRow, Env } from "../types";
import { CORS_HEADERS, BSKY_TYPEAHEAD_URL } from "../types";
import { json, sanitize, avatarUrl, avatarFallbackUrl } from "../utils";
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

        // An EMPTY result from the replica falls through to Turso.
        //
        // The replica is a lagging copy: an actor written to Turso is invisible
        // to it until the next sync tick, and if that sync stalls, invisible for
        // as long as the stall lasts (2026-07-28: 48 hours). Turso is the
        // authority and always has the row, so when the fast path finds nothing
        // it is worth paying for the slow path before answering "no such actor".
        //
        // Only on empty, so the common case still costs one backend hop. A
        // genuinely unmatched query pays the Turso round trip once per 60s
        // cache window, since the empty answer is cached like any other.
        let empty = false;
        try {
          empty = (JSON.parse(body).actors ?? []).length === 0;
        } catch {
          empty = false;   // unparseable is the backend's problem, not a miss
        }
        if (empty) throw new Error("backend_empty");

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
      // unreachable backend, timeout, or an empty result — Turso answers
    }
  }

  // fallback: 3-tier ranking via turso. Overfetch prefix + FTS so the
  // interleaved merge below has real candidates to pick from.
  const ftsQuery = `"${term}"*`;
  const overfetch = Math.min(limit * 5, 50);
  // Bounded, like the backend hop above it.
  //
  // This path now runs whenever the replica answers empty, not just when it is
  // unreachable — so a slow Turso is on the hot path for every miss. During the
  // 2026-07-30 resync that meant a 45s hang on a query the replica simply did
  // not have yet. Answering "nothing found" quickly beats answering correctly
  // eventually: the caller is a typeahead, and the next keystroke is 200ms away.
  const FALLBACK_BUDGET_MS = 2500;
  const batch = db.batch([
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
       FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1`
    ).bind(term),
    db.prepare(
      `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
       FROM actors WHERE handle LIKE ?1 COLLATE NOCASE AND handle != ?2 COLLATE NOCASE AND hidden = 0
       LIMIT ?3`
    ).bind(term + '%', term, overfetch),
    db.prepare(
      `SELECT a.did, a.handle, a.display_name, a.avatar_url, a.labels, a.created_at, a.associated, a.pds
       FROM actors_fts
       JOIN actors a ON a.rowid = actors_fts.rowid
       WHERE actors_fts MATCH ?1 AND a.handle != '' AND a.hidden = 0
       ORDER BY rank
       LIMIT ?2`
    ).bind(ftsQuery, overfetch),
  ], "read");

  let exactRes, prefixRes, ftsRes;
  try {
    [exactRes, prefixRes, ftsRes] = await Promise.race([
      batch,
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error("turso_fallback_timeout")), FALLBACK_BUDGET_MS)),
    ]);
  } catch (e: any) {
    console.log(JSON.stringify({ event: "search_fallback_timeout", term, error: e?.message }));
    // deliberately NOT cached — a timeout is a transient property of the
    // moment, and caching it would pin an empty answer for 60s
    return json({ actors: [] });
  }

  // Sort prefix by handle length (shortest first) for the shortest-handle bias.
  // We dropped ORDER BY length(handle) from the SQL to avoid Turso's full-scan
  // sort; doing it in JS over <= 50 rows is cheap.
  const prefixRows = ((prefixRes.results as ActorRow[]) || [])
    .slice()
    .sort((a, b) => a.handle.length - b.handle.length);
  const exactRows = (exactRes.results as ActorRow[]) || [];

  // Dedupe FTS candidates against the prefix candidate pool. Without this,
  // FTS rank tends to return the same short-handle rows the prefix tier
  // already has, those slots collapse to dupes on emit, and display-name-only
  // matches (e.g. bad-example.com / "fig (aka:[phil])" for q=fig) never
  // contribute to the response.
  const prefixDids = new Set(prefixRows.map((r) => r.did));
  const ftsRows = ((ftsRes.results as ActorRow[]) || [])
    .filter((r) => !prefixDids.has(r.did));

  // Interleave prefix and FTS so display-name-only matches surface even when
  // there are >= limit handle-prefix matches. Without this, q=fig with many
  // 'fig*' handles starves the FTS tier — bad-example.com (displayName
  // 'fig (aka:[phil])') never appears.
  const seen = new Set<string>();
  const merged: ActorRow[] = [];
  for (const row of exactRows) {
    if (seen.has(row.did)) continue;
    seen.add(row.did);
    merged.push(row);
    if (merged.length >= limit) break;
  }
  let pi = 0, fi = 0;
  let takePrefix = true; // bias: handle-prefix gets the first non-exact slot
  while (merged.length < limit && (pi < prefixRows.length || fi < ftsRows.length)) {
    let row: ActorRow | null = null;
    if (takePrefix) {
      while (pi < prefixRows.length && seen.has(prefixRows[pi].did)) pi++;
      if (pi < prefixRows.length) {
        row = prefixRows[pi++];
      } else {
        while (fi < ftsRows.length && seen.has(ftsRows[fi].did)) fi++;
        if (fi < ftsRows.length) row = ftsRows[fi++];
      }
    } else {
      while (fi < ftsRows.length && seen.has(ftsRows[fi].did)) fi++;
      if (fi < ftsRows.length) {
        row = ftsRows[fi++];
      } else {
        while (pi < prefixRows.length && seen.has(prefixRows[pi].did)) pi++;
        if (pi < prefixRows.length) row = prefixRows[pi++];
      }
    }
    if (!row) break;
    seen.add(row.did);
    merged.push(row);
    takePrefix = !takePrefix;
  }

  const actors = merged.map((r) => ({
    did: r.did,
    handle: r.handle,
    ...(r.display_name ? { displayName: r.display_name } : {}),
    ...(r.avatar_url ? { avatar: avatarUrl(r.did, r.avatar_url, r.pds) } : {}),
    ...(r.avatar_url && avatarFallbackUrl(r.did, r.avatar_url, r.pds) ? { avatarFallback: avatarFallbackUrl(r.did, r.avatar_url, r.pds) } : {}),
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

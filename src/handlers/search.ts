import { normalizePds } from "../pds";
export { normalizePds } from "../pds";
import { rowsAs, type TursoDB } from "../db";
import type { ActorRow, Env } from "../types";
import { CORS_HEADERS, BSKY_TYPEAHEAD_URL } from "../types";
import { json, sanitize, avatarUrl, avatarFallbackUrl, type ProfileAssociated } from "../utils";
import { recordMetric, recordCacheHit, recordTrafficSource } from "../metrics";
import { throttledBackfill } from "../backfill";

/** one actor in the response, keys in the order the wire contract serializes them */
interface ActorView {
  did: string;
  handle: string;
  displayName?: string;
  avatar?: string;
  avatarFallback?: string;
  associated?: ProfileAssociated;
  labels?: unknown[];
  createdAt?: string;
}

/** Largest `limit` for which an empty engine answer is re-asked of Turso.
 *
 *  The Turso fallback is a full-corpus FTS query, the one corpus-proportional
 *  read left on the request path. A person typing a handle asks for 8-10 rows
 *  and deserves that second look when the engine has nothing yet. A bulk
 *  client asking for 100 rows per rare keyword is not looking for one actor
 *  that might be seconds old, and one nightly job doing exactly that (200
 *  Belgian-vocabulary searches in two minutes, 2026-08-17 onward) put every
 *  other user's median wall time from 282 ms to 1.4 s while it ran.
 *  docs/operations.md "unattributed bulk clients". */
export const TURSO_FALLBACK_MAX_LIMIT = 25;

/** Off unless `TURSO_FALLBACK_ON_EMPTY=1` is set on the worker.
 *
 *  2026-09-04: an adopter went viral, engine volume rose 20x, and every empty
 *  engine answer became a full-corpus FTS query holding a Turso connection for
 *  up to 2.5 s. Turso hit its connection limit (1,310 requests in 90 min),
 *  the ingester's /admin/ingest writes failed behind it, and its backlog
 *  overflowed — search traffic dropped discovery events. The reason the
 *  fallback existed (a lagging replica) is gone since 2026-09-02: the search
 *  box has no replica, and request-indexing pushes the row into its overlay
 *  synchronously (`admin.ts` → `/admin/refresh`). Turso still answers when
 *  the engine is unreachable; it no longer answers when the engine says no.
 *  docs/operations.md "unattributed bulk clients". */
export function tursoFallbackOnEmpty(limit: number, enabled: boolean): boolean {
  return enabled && limit <= TURSO_FALLBACK_MAX_LIMIT;
}

/** Whether an unreachable or erroring engine sends the query to Turso.
 *
 *  Off unless `TURSO_FALLBACK_ON_UNREACHABLE=1`. The engine is one machine
 *  that adopts its on-disk snapshot in ~15 s after a restart; during the
 *  2026-09-04 burst one such restart routed every query to the full-corpus
 *  Turso path for those 15 s, which was enough to put Turso over its
 *  connection limit and make the ingester drop events. An empty answer for
 *  15 s costs a typeahead nothing it will remember. */
export function tursoFallbackOnUnreachable(enabled: boolean): boolean {
  return enabled;
}


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

  const rawPds = url.searchParams.get("pds");
  const pds = rawPds === null ? null : normalizePds(rawPds);
  if (rawPds !== null && pds === null) {
    return json({ error: "InvalidRequest", message: "pds must be a DNS hostname or HTTP(S) origin without a port" }, 400);
  }
  const unavailable = () => json({ error: "PdsScopeUnavailable", message: "PDS-scoped search is unavailable" }, 503);
  if (pds && (env.PDS_SEARCH_DISABLED === "1" || env.EMERGENCY_PROXY_BSKY || !env.SEARCH_BACKEND_URL)) return unavailable();

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
  const ingressColo = request.cf?.colo ?? "unknown";

  // cache API — edge cache for hot queries
  // hostname must belong to this zone so the CF zone-purge API can target it
  const host = new URL(request.url).host;
  const cacheKey = new Request(
    `https://${host}/__cache/${encodeURIComponent(term)}:${limit}${pds ? `?pds=${encodeURIComponent(pds)}` : ""}`,
    request
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    ctx.waitUntil(recordCacheHit(db, Date.now() - t0));
    const response = new Response(cached.body, cached);
    response.headers.set("Server-Timing", `cache;desc="hit";dur=${Date.now() - t0}`);
    return response;
  }

  const cacheMs = Date.now() - t0;

  // try fly backend (local SQLite replica) if configured
  if (env.SEARCH_BACKEND_URL) {
    let backendStrategy: "retained" | "proof" | "unknown" = "unknown";
    let backendRouteClass: "routed" | "direct" | "other" = "other";
    try {
      const backendUrl = `${env.SEARCH_BACKEND_URL}/search?q=${encodeURIComponent(term)}&limit=${limit}${pds ? `&pds=${encodeURIComponent(pds)}` : ""}`;
      const parsedBackend = new URL(backendUrl);
      backendRouteClass = parsedBackend.origin === "https://typeahead-search-transport.n8-3e9.workers.dev" ? "routed"
        : parsedBackend.hostname.endsWith(".fly.dev") ? "direct" : "other";
      const backendStart = Date.now();
      const init = { signal: AbortSignal.timeout(3000) };
      const backendRes = env.SEARCH_TRANSPORT
        ? await env.SEARCH_TRANSPORT.fetch(backendUrl, init)
        : await fetch(backendUrl, init);
      backendStrategy = backendRes.headers.get("X-Typeahead-Route-Strategy") === "proof" ? "proof" : "retained";
      if (pds && backendRes.headers.get("X-Typeahead-Pds") !== pds) return unavailable();
      if (!backendRes.ok) throw new Error(`backend_${backendRes.status}`);
      {
        const headersMs = Date.now() - backendStart;
        const body = await backendRes.text();
        const backendMs = Date.now() - backendStart;

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
        if (!pds && empty && tursoFallbackOnEmpty(limit, env.TURSO_FALLBACK_ON_EMPTY === "1")) throw new Error("backend_empty");

        const elapsedMs = Date.now() - t0;
        ctx.waitUntil(Promise.all([
          recordMetric(db, elapsedMs),
          recordTrafficSource(db, request),
        ]));
        const timing = backendRes.headers.get("Server-Timing") ?? "";
        const routeMs = /transport_route;dur=([0-9.]+)/.exec(timing);
        const originMs = /transport_origin_headers;dur=([0-9.]+)/.exec(timing);
        console.log(JSON.stringify({ event: "search_response_timing", outcome: "ok", status: 200,
          backend_strategy: backendStrategy, backend_route_class: backendRouteClass, ingress_colo: ingressColo,
          search_ms: elapsedMs, cache_ms: cacheMs, backend_headers_ms: headersMs, backend_ms: backendMs,
          transport_route_ms: routeMs ? Number(routeMs[1]) : null,
          transport_origin_ms: originMs ? Number(originMs[1]) : null, empty }));

        const response = new Response(body, {
          headers: { "Content-Type": "application/json", ...CORS_HEADERS,
            "Server-Timing": `cache;desc="miss";dur=${cacheMs}, origin_headers;dur=${headersMs};desc="${backendRes.headers.get("fly-request-id") || "unknown"}", origin;dur=${backendMs}, search;dur=${Date.now() - t0}` },
        });

        const cacheable = new Response(response.clone().body, response);
        cacheable.headers.set("Cache-Control", "public, max-age=60");
        ctx.waitUntil(cache.put(cacheKey, cacheable));

        return response;
      }
    } catch (e: unknown) {
      if (pds) return unavailable();
      const reason = e instanceof Error ? e.message : String(e);
      if (reason !== "backend_empty" && !tursoFallbackOnUnreachable(env.TURSO_FALLBACK_ON_UNREACHABLE === "1")) {
        console.log(JSON.stringify({ event: "search_response_timing", outcome: "backend_error", status: 200,
          backend_strategy: backendStrategy, backend_route_class: backendRouteClass, ingress_colo: ingressColo,
          search_ms: Date.now() - t0, cache_ms: cacheMs }));
        console.log(JSON.stringify({ event: "search_backend_unreachable", term, error: reason }));
        // not cached: the engine restarts in ~15 s and the next keystroke
        // should reach it, not a pinned empty answer
        return json({ actors: [] });
      }
      // opted in: Turso answers below
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
  const prefixRows = rowsAs<ActorRow>(prefixRes.results)
    .slice()
    .sort((a, b) => a.handle.length - b.handle.length);
  const exactRows = rowsAs<ActorRow>(exactRes.results);

  // Dedupe FTS candidates against the prefix candidate pool. Without this,
  // FTS rank tends to return the same short-handle rows the prefix tier
  // already has, those slots collapse to dupes on emit, and display-name-only
  // matches (e.g. bad-example.com / "fig (aka:[phil])" for q=fig) never
  // contribute to the response.
  const prefixDids = new Set(prefixRows.map((r) => r.did));
  const ftsRows = rowsAs<ActorRow>(ftsRes.results)
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

  const actors = merged.map((r) => {
    const view: ActorView = { did: r.did, handle: r.handle };
    if (r.display_name) view.displayName = r.display_name;
    if (r.avatar_url) {
      view.avatar = avatarUrl(r.did, r.avatar_url, r.pds);
      const fallback = avatarFallbackUrl(r.did, r.avatar_url, r.pds);
      if (fallback) view.avatarFallback = fallback;
    }
    if (r.associated && r.associated !== '{}') view.associated = JSON.parse(r.associated);
    view.labels = JSON.parse(r.labels || '[]');
    if (r.created_at) view.createdAt = r.created_at;
    return view;
  });

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

import { filterIdentityBody, filterIdentityResponse } from "../identity-policy";
import { parseSearchScope, scopeAcknowledged, scopeParams, searchCacheGeneration, searchCacheUrl, validateBackendBody, type SearchScope } from "./search-contract";
export { normalizePds } from "../pds";
import type { SqlDb } from "../db";
import type { Env } from "../types";
import { CORS_HEADERS, BSKY_TYPEAHEAD_URL } from "../types";
import { json, sanitize } from "../utils";
import { recordMetric, recordCacheHit, recordTrafficSource } from "../metrics";
import { recordStats } from "../stats-store";

async function blueskyFallback(term: string, limit: number, mode: "manual" | "automatic"): Promise<Response> {
  const headers = { ...CORS_HEADERS, "Content-Type": "application/json", "Cache-Control": "no-store",
    "X-Typeahead-Engine": "bluesky", "X-Typeahead-Failover": mode,
    "Access-Control-Expose-Headers": "X-Typeahead-Engine, X-Typeahead-Failover" };
  let status = 503;
  try {
    const upstream = await fetch(`${BSKY_TYPEAHEAD_URL}?q=${encodeURIComponent(term)}&limit=${limit}`,
      { signal: AbortSignal.timeout(5000), redirect: "manual" });
    if (!upstream.ok) {
      await upstream.body?.cancel();
      throw new Error("Bluesky unavailable");
    }
    const reader = upstream.body?.getReader();
    if (!reader) throw new Error("Missing Bluesky response");
    const decoder = new TextDecoder();
    let body = "";
    let bytes = 0;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > 1024 * 1024) throw new Error("Bluesky response exceeds limit");
        body += decoder.decode(chunk.value, { stream: true });
      }
      body += decoder.decode();
    } finally { await reader.cancel().catch(() => {}); }
    validateBackendBody(body, limit);
    status = 200;
    return new Response(filterIdentityBody(body, "actors"), { headers });
  } catch (error) {
    console.log(JSON.stringify({event: "search_failover_error", error: error instanceof Error ? error.name : "unknown"}));
    return new Response(JSON.stringify({ error: "SearchUnavailable", message: "Search and emergency fallback are unavailable" }), { status, headers });
  } finally {
    console.log(JSON.stringify({ event: "search_failover", mode, provider: "bluesky", status }));
  }
}

export async function handleSearch(
  request: Request,
  db: SqlDb,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const q = url.searchParams.get("q") || url.searchParams.get("term") || "";
  const limitRaw = url.searchParams.get("limit");
  const limitParam = limitRaw !== null && /^\d+$/.test(limitRaw) ? Number(limitRaw) : limitRaw === null ? 10 : NaN;

  if (!q.trim()) {
    return json({ error: "InvalidRequest", message: "Error: Params must have the property \"q\"" }, 400);
  }
  if (isNaN(limitParam) || limitParam < 1 || limitParam > 100) {
    return json({ error: "InvalidRequest", message: "Error: limit must be between 1 and 100" }, 400);
  }

  let scope: SearchScope;
  try { scope = parseSearchScope(url.searchParams); }
  catch (error) { return json({ error: "InvalidRequest", message: error instanceof Error ? error.message : "Invalid PDS parameters" }, 400); }
  const { pds, preference } = scope;
  const scoped = Boolean(pds || preference);
  const unavailable = () => json({ error: "PdsScopeUnavailable", message: "PDS-scoped search is unavailable" }, 503);
  if (scoped && (env.PDS_SEARCH_DISABLED === "1" || env.EMERGENCY_PROXY_BSKY || !env.SEARCH_BACKEND_URL)) return unavailable();

  const limit = limitParam;
  const t0 = Date.now();
  const term = sanitize(q);
  if (!term) {
    recordStats(ctx, env, db, (store, now) => recordMetric(store, now - t0, now));
    return json({ actors: [] });
  }

  const fallback = async (mode: "manual" | "automatic") => {
    const response = await blueskyFallback(term, limit, mode);
    recordStats(ctx, env, db, (store, now) => recordMetric(store, now - t0, now));
    recordStats(ctx, env, db, (store, now) => recordTrafficSource(store, request, now));
    return response;
  };
  if (env.EMERGENCY_PROXY_BSKY) return fallback("manual");
  if (!env.SEARCH_BACKEND_URL) return fallback("automatic");

  const ingressColo = request.cf?.colo ?? "unknown";

  // cache API — edge cache for hot queries
  // hostname must belong to this zone so the CF zone-purge API can target it
  const host = new URL(request.url).host;
  let cacheGeneration: string;
  try { cacheGeneration = searchCacheGeneration(env.SEARCH_CACHE_GENERATION); }
  catch { return json({ error: "SearchConfigurationUnavailable" }, 503); }
  const cacheKey = new Request(
    searchCacheUrl(host, term, limit, scope, cacheGeneration),
    request
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    recordStats(ctx, env, db, (store, now) => recordCacheHit(store, now - t0, now));
    const response = await filterIdentityResponse(cached, "actors");
    response.headers.set("Server-Timing", `cache;desc="hit";dur=${Date.now() - t0}`);
    return response;
  }

  const cacheMs = Date.now() - t0;

  try {
    let backendStrategy: "retained" | "proof" | "unknown" = "unknown";
    let backendRouteClass: "routed" | "direct" | "other" = "other";
    try {
      const extra = scopeParams(scope).toString();
      const backendUrl = `${env.SEARCH_BACKEND_URL}/search?q=${encodeURIComponent(term)}&limit=${limit}${extra ? `&${extra}` : ""}`;
      const parsedBackend = new URL(backendUrl);
      backendRouteClass = parsedBackend.origin === "https://typeahead-search-transport.n8-3e9.workers.dev" ? "routed"
        : parsedBackend.hostname.endsWith(".fly.dev") ? "direct" : "other";
      const backendStart = Date.now();
      const init = { signal: AbortSignal.timeout(3000) };
      const backendRes = env.SEARCH_TRANSPORT
        ? await env.SEARCH_TRANSPORT.fetch(backendUrl, init)
        : await fetch(backendUrl, init);
      backendStrategy = backendRes.headers.get("X-Typeahead-Route-Strategy") === "proof" ? "proof" : "retained";
      if (backendRes.status === 400) {
        await backendRes.body?.cancel().catch(() => {});
        return json({ error: "InvalidRequest", message: "Search engine rejected the query or parameters" }, 400);
      }
      if (scoped && !scopeAcknowledged(scope, backendRes.headers)) {
        await backendRes.body?.cancel().catch(() => {});
        return unavailable();
      }
      if (!backendRes.ok) {
        await backendRes.body?.cancel().catch(() => {});
        if (scoped) return unavailable();
        return fallback("automatic");
      }
      {
        const headersMs = Date.now() - backendStart;
        const upstreamBody = await backendRes.text();
        validateBackendBody(upstreamBody, limit);
        const body = filterIdentityBody(upstreamBody, "actors");
        const backendMs = Date.now() - backendStart;

        let empty = false;
        try {
          empty = (JSON.parse(body).actors ?? []).length === 0;
        } catch {
          empty = false;   // unparseable is the backend's problem, not a miss
        }

        const elapsedMs = Date.now() - t0;
        recordStats(ctx, env, db, (store, now) => recordTrafficSource(store, request, now));
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
        response.headers.set("X-Typeahead-Cache-Generation", cacheGeneration);
        for (const header of ["X-Typeahead-Engine", "X-Typeahead-Format", "X-Typeahead-Build", "X-Typeahead-Binary-Sha256", "X-Typeahead-Retrieval-Strategy", "X-Typeahead-Machine-Id", "X-Typeahead-Machine-Version"]) {
          const value = backendRes.headers.get(header);
          if (value !== null) response.headers.set(header, value);
        }

        const cacheable = new Response(response.clone().body, response);
        cacheable.headers.set("Cache-Control", "public, max-age=60");
        ctx.waitUntil(cache.put(cacheKey, cacheable));

        return response;
      }
    } catch (e: unknown) {
      if (scoped) return unavailable();
      console.log(JSON.stringify({ event: "search_response_timing", outcome: "backend_error", status: 503,
        backend_strategy: backendStrategy, backend_route_class: backendRouteClass, ingress_colo: ingressColo,
        search_ms: Date.now() - t0, cache_ms: cacheMs }));
      console.log(JSON.stringify({ event: "search_backend_unreachable", term, error: e instanceof Error ? e.message : String(e) }));
      return fallback("automatic");
    }
  } finally {
    recordStats(ctx, env, db, (store, now) => recordMetric(store, now - t0, now));
  }
}

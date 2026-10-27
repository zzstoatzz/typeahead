import { allowedRoute, readRoute, routeHintRecord, sharedHintsAllowed, speculativeRoute } from "./search-route";
import { HINT_CACHE, readSharedHintRecord, reuseVerifiedRoute, rememberSharedHint, forgetSharedHint } from "./shared-route-hint";
import { validatedSearch } from "./validated-search";

const origins = new Set([
  "https://typeahead-search-compact.fly.dev",
  "https://typeahead-search.fly.dev",
  "https://typeahead-startup-20261024.fly.dev",
]);

export default {
  async fetch(request: Request, env: SearchTransportEnv, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (origins.has(url.origin)) {
      if (request.method !== "GET" || url.pathname !== "/search") return new Response("Not found", { status: 404 });
      return fetch(url, { signal: AbortSignal.timeout(3000) });
    }
    if (url.origin !== "https://typeahead-search-transport.n8-3e9.workers.dev" || !allowedRoute(request)) {
      return new Response("Not found", { status: 404 });
    }
    if (request.method === "GET" && url.pathname === "/search") {
      const current = readRoute(env.SEARCH_CONTROL, true);
      const memoryHint = speculativeRoute();
      const cache = Promise.resolve().then(() => caches.open(HINT_CACHE)).catch(() => undefined);
      const shared = sharedHintsAllowed() ? cache.then(value => value ? readSharedHintRecord(value) : undefined) : Promise.resolve(undefined);
      const hint = memoryHint ?? shared.then(record => record?.route);
      ctx.waitUntil(Promise.allSettled([
        Promise.resolve(hint),
        current.then(() => cache.then(value => value ? rememberSharedHint(value, routeHintRecord()) : undefined),
          () => cache.then(value => value ? forgetSharedHint(value) : undefined)),
      ]).then(() => undefined));
      const response = await validatedSearch(reuseVerifiedRoute(current, memoryHint, shared), hint, (route, signal) => {
        const forwarded = new Request(new URL(url.pathname + url.search, route.origin), request);
        forwarded.headers.delete("Host");
        forwarded.headers.delete("X-Typeahead-Routed-Health");
        return fetch(forwarded, { signal, redirect: "manual" });
      }, memoryHint ? "memory" : "shared");
      response.headers.set("X-Typeahead-Route-Strategy", "proof");
      return response;
    }
    let route;
    try {
      route = await readRoute(env.SEARCH_CONTROL, request.method === "GET" && url.pathname !== "/route");
    } catch {
      return Response.json({ error: "SearchRouteUnavailable" }, { status: 503 });
    }
    if (url.pathname === "/route") return Response.json(route, { headers: { "Cache-Control": "no-store" } });
    const target = new URL(url.pathname + url.search, route.origin);
    const forwarded = new Request(target, request);
    forwarded.headers.delete("Host");
    forwarded.headers.delete("X-Typeahead-Routed-Health");
    if (url.pathname === "/health") forwarded.headers.set("X-Typeahead-Routed-Health", "1");
    try {
      return await fetch(forwarded, { signal: AbortSignal.timeout(url.pathname === "/admin/refresh" ? 5000 : 3000), redirect: "manual" });
    } catch {
      return Response.json({ error: "SearchBackendUnavailable" }, { status: 503 });
    }
  },
};

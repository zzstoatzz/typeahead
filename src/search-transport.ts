import { allowedRoute, readRoute, speculativeRoute } from "./search-route";
import { validatedSearch } from "./validated-search";

const origins = new Set([
  "https://typeahead-search-compact.fly.dev",
  "https://typeahead-search.fly.dev",
  "https://typeahead-startup-20261024.fly.dev",
]);

export default {
  async fetch(request: Request, env: SearchTransportEnv): Promise<Response> {
    const url = new URL(request.url);
    if (origins.has(url.origin)) {
      if (request.method !== "GET" || url.pathname !== "/search") return new Response("Not found", { status: 404 });
      return fetch(url, { signal: AbortSignal.timeout(3000) });
    }
    if (url.origin !== "https://typeahead-search-transport.n8-3e9.workers.dev" || !allowedRoute(request)) {
      return new Response("Not found", { status: 404 });
    }
    if (request.method === "GET" && url.pathname === "/search") {
      return validatedSearch(readRoute(env.SEARCH_CONTROL, true), speculativeRoute(), (route, signal) => {
        const forwarded = new Request(new URL(url.pathname + url.search, route.origin), request);
        forwarded.headers.delete("Host");
        forwarded.headers.delete("X-Typeahead-Routed-Health");
        return fetch(forwarded, { signal, redirect: "manual" });
      });
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

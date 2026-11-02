import { allowedRoute, readRoute, routeHintRecord, sharedHintsAllowed, speculativeRoute, responseMatchesRoute } from "./search-route";
import { HINT_CACHE, readSharedHint, rememberSharedHint, forgetSharedHint } from "./shared-route-hint";
import { validatedSearch } from "./validated-search";

export { RouteAuthority } from "./route-authority";

// without the binding (isolated harnesses) each isolate reads R2 itself, as before
function routeSource(env: SearchTransportEnv) {
  const authority: SearchTransportEnv["ROUTE_AUTHORITY"] | undefined = env.ROUTE_AUTHORITY;
  // An object stays where it was first created. "route" was created with the
  // enam hint and landed in MIA, ~35 ms from the transport's IAD placement;
  // created without a hint, an object is placed near its first caller.
  return authority?.get(authority.idFromName("route-2"));
}

export default {
  async fetch(request: Request, env: SearchTransportEnv, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.origin !== "https://typeahead-search-transport.n8-3e9.workers.dev" || !allowedRoute(request)) {
      return new Response("Not found", { status: 404 });
    }
    if (request.method === "GET" && url.pathname === "/search") {
      const current = readRoute(env.SEARCH_CONTROL, true, routeSource(env));
      const memoryHint = speculativeRoute();
      const cache = Promise.resolve().then(() => caches.open(HINT_CACHE)).catch(() => undefined);
      const hint = memoryHint ?? (sharedHintsAllowed() ? cache.then(value => value ? readSharedHint(value) : undefined) : undefined);
      ctx.waitUntil(Promise.allSettled([
        Promise.resolve(hint),
        current.then(() => cache.then(value => value ? rememberSharedHint(value, routeHintRecord()) : undefined),
          () => cache.then(value => value ? forgetSharedHint(value) : undefined)),
      ]).then(() => undefined));
      return validatedSearch(current, hint, (route, signal) => {
        const forwarded = new Request(new URL(url.pathname + url.search, route.origin), request);
        forwarded.headers.delete("Host");
        forwarded.headers.delete("X-Typeahead-Routed-Health");
        return fetch(forwarded, { signal, redirect: "manual" });
      }, memoryHint ? "memory" : "shared");
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
      const response = await fetch(forwarded, { signal: AbortSignal.timeout(3000), redirect: "manual" });
      if (!responseMatchesRoute(route, response)) {
        await response.body?.cancel().catch(() => {});
        return Response.json({ error: "SearchBackendIdentityMismatch" }, { status: 503 });
      }
      return response;
    } catch {
      return Response.json({ error: "SearchBackendUnavailable" }, { status: 503 });
    }
  },
};

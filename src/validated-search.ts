import { routeIdentity, responseMatchesRoute, type SearchRoute as Route } from "./search-route";
import { timingDurations } from "./server-timing";

type OriginFetch = (route: Route, signal: AbortSignal) => Promise<Response>;

export async function validatedSearch(
  current: Promise<Route>,
  hint: Route | undefined | Promise<Route | undefined>,
  fetchOrigin: OriginFetch,
  hintSource: "memory" | "shared" = "memory",
): Promise<Response> {
  const started = Date.now();
  const report = (status: number, mode: string, routeMs: number, originMs: number, outcome: string) => {
    console.log(JSON.stringify({ event: "search_transport_timing", outcome,
      status, mode, route_ms: routeMs, origin_headers_ms: originMs, total_headers_ms: Date.now() - started,
      hint_source: selectedHint ? hintSource : "none", hint_wait_ms: hintWaitMs, sample_rate: 1 }));
  };
  const begin = (route: Route) => {
    const abort = new AbortController();
    const originStarted = Date.now();
    const offsetMs = originStarted - started;
    const result = fetchOrigin(route, AbortSignal.any([abort.signal, AbortSignal.timeout(3000)]))
      .then(response => ({ ok: true as const, response, headersMs: Date.now() - originStarted }),
        () => ({ ok: false as const, headersMs: Date.now() - originStarted }));
    return { route, abort, result, offsetMs };
  };
  const discard = async (pending: ReturnType<typeof begin>) => {
    pending.abort.abort();
    const result = await pending.result;
    if (result.ok) await result.response.body?.cancel().catch(() => {});
    return result.headersMs;
  };
  const selectedHint = await Promise.race([Promise.resolve(hint), current.then(() => undefined, () => undefined)]);
  const hintWaitMs = Date.now() - started;
  let pending = selectedHint ? begin(selectedHint) : undefined;
  let route: Route;
  try {
    route = await current;
  } catch {
    const routeMs = Date.now() - started;
    const originMs = pending ? await discard(pending) : 0;
    report(503, selectedHint ? "overlap" : "serial", routeMs, originMs, "route_unavailable");
    return Response.json({ error: "SearchRouteUnavailable" }, { status: 503 });
  }
  const routeMs = Date.now() - started;
  let mode = pending ? "overlap" : "serial";
  if (pending && routeIdentity(pending.route) !== routeIdentity(route)) {
    await discard(pending);
    pending = undefined;
    mode = "reselected";
  }
  pending ??= begin(route);
  const result = await pending.result;
  if (!result.ok) {
    report(503, mode, routeMs, result.headersMs, "backend_unavailable");
    return Response.json({ error: "SearchBackendUnavailable" }, { status: 503 });
  }
  report(result.response.status, mode, routeMs, result.headersMs, "http");
  const response = new Response(result.response.body, result.response);
  if (!responseMatchesRoute(route, response)) {
    await response.body?.cancel().catch(() => {});
    return Response.json({ error: "SearchBackendIdentityMismatch" }, { status: 503 });
  }
  const engineMs = timingDurations(response.headers.get("Server-Timing")).get("search");
  response.headers.set("Server-Timing", `transport_route;dur=${routeMs}, transport_origin_headers;dur=${result.headersMs}, transport_total;dur=${Date.now() - started}, transport_hint_wait;dur=${hintWaitMs}, transport_origin_start;dur=${pending.offsetMs}, transport_mode;desc="${mode}"${engineMs === undefined ? "" : `, engine;dur=${engineMs}`}`);
  return response;
}

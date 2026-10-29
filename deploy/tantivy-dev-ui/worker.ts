const BACKEND = "https://typeahead-tantivy-dev.fly.dev";
const backendPaths = new Set([
  "/search",
  "/debug/search",
  "/health",
  "/ready",
  "/index/status",
  "/index-status",
  "/xrpc/tech.waow.typeahead.searchActors",
  "/xrpc/app.bsky.actor.searchActorsTypeahead",
]);

function previewResponse(upstream: Response): Response {
  const response = new Response(upstream.body, upstream);
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("X-Typeahead-Search-Backend", BACKEND);
  return response;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return Response.json({ error: "DevReadOnly", message: "This dev preview is read-only. Indexing and administrator mutations are disabled." }, { status: 503, headers: { "Cache-Control": "no-store" } });
    }
    if (backendPaths.has(url.pathname)) {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return new Response("Method not allowed", { status: 405 });
      }
      try {
        return previewResponse(await fetch(new URL(url.pathname + url.search, BACKEND), {
          method: request.method,
          headers: { Accept: "application/json" },
          signal: AbortSignal.timeout(5000),
          redirect: "manual",
        }));
      } catch {
        return Response.json({ error: "SearchBackendUnavailable" }, { status: 503, headers: { "Cache-Control": "no-store" } });
      }
    }
    if (url.pathname.startsWith("/admin")) {
      return new Response("Not found", { status: 404 });
    }
    url.protocol = "https:";
    url.hostname = "typeahead.waow.tech";
    url.port = "";
    return previewResponse(await env.PRODUCTION_UI.fetch(new Request(url, request)));
  },
} satisfies ExportedHandler<Env>;

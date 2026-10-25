const origins = new Set([
  "https://typeahead-search-compact.fly.dev",
  "https://typeahead-search.fly.dev",
  "https://typeahead-startup-20261024.fly.dev",
]);

export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.method !== "GET" || url.pathname !== "/search" || !origins.has(url.origin)) {
      return new Response("Not found", { status: 404 });
    }
    return fetch(url, { signal: AbortSignal.timeout(3000) });
  },
};

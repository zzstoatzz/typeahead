import { trace } from "../worker-tracing";

interface ProbeEnv {
  REGIONAL: Fetcher;
  DEFAULT: Fetcher;
  CURRENT: Fetcher;
}

export default {
  async fetch(request: Request, env: ProbeEnv, ctx: ExecutionContext) {
    const url = new URL(request.url);
    const variant = url.searchParams.get("variant");
    const mode = url.searchParams.get("mode");
    const q = url.searchParams.get("q") ?? "zzstoatzz.io";
    if (request.method !== "GET" || url.pathname !== "/probe" ||
      (variant !== "regional" && variant !== "default" && variant !== "current") ||
      (mode !== "echo" && mode !== "search") ||
      (mode === "echo" && variant === "current") ||
      !["zzstoatzz.io", "na", "aoc"].includes(q)) return new Response(null, { status: 404 });
    const target = variant === "regional" ? env.REGIONAL : variant === "default" ? env.DEFAULT : env.CURRENT;
    const id = crypto.randomUUID();
    const targetUrl = new URL(mode === "echo" ? "/echo" : "/search", "https://typeahead-search-transport.n8-3e9.workers.dev");
    if (mode === "search") targetUrl.search = new URLSearchParams({ q, limit: "7" }).toString();
    return trace(ctx.tracing, "probe.call", async span => {
      span?.setAttribute("probe.id", id);
      span?.setAttribute("probe.variant", variant);
      span?.setAttribute("probe.mode", mode);
      const start = Date.now();
      const response = await target.fetch(targetUrl, { headers: { "X-Typeahead-Request-Id": id, "X-Client": "bench" },
        signal: AbortSignal.timeout(5000) });
      const headersAt = Date.now();
      const body = await response.text();
      const end = Date.now();
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body)))]
        .map(byte => byte.toString(16).padStart(2, "0")).join("");
      const result = { id, variant, mode, q: mode === "search" ? q : null, ingress: request.cf?.colo,
        status: response.status, headersMs: headersAt - start, bodyMs: end - headersAt,
        totalMs: end - start, handlerMs: response.headers.get("X-Probe-Handler-Ms"),
        placement: response.headers.get("X-Probe-Placement"), serverTiming: response.headers.get("Server-Timing"),
        build: response.headers.get("X-Typeahead-Build"), hash, bytes: body.length };
      console.log({ event: "placement_probe", ...result });
      return Response.json(result, { headers: { "Cache-Control": "no-store" } });
    });
  },
} satisfies ExportedHandler<ProbeEnv>;

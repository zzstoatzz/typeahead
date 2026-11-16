import transport from "../search-transport";

const payload = JSON.stringify({ actors: [], padding: "x".repeat(3072) });

export default {
  async fetch(request: Request, env: SearchTransportEnv, ctx: ExecutionContext) {
    const started = Date.now();
    const placement = request.headers.get("cf-placement") ?? "unreported";
    if (new URL(request.url).pathname === "/echo") {
      return new Response(payload, { headers: {
        "Content-Type": "application/json",
        "X-Probe-Handler-Ms": String(Date.now() - started),
        "X-Probe-Placement": placement,
      } });
    }
    const response = await transport.fetch(request, env, ctx);
    const result = new Response(response.body, response);
    result.headers.set("X-Probe-Handler-Ms", String(Date.now() - started));
    result.headers.set("X-Probe-Placement", placement);
    return result;
  },
} satisfies ExportedHandler<SearchTransportEnv>;

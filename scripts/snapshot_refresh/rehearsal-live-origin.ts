import { parseRoute } from "../../src/search-route";
import { z } from "zod";

export function liveConfiguration(value: unknown) {
  const input = z.object({ schema_version: z.literal(1), native: z.unknown(), candidate: z.unknown() }).strict().parse(value);
  const native = parseRoute({ version: 1, current: input.native });
  const candidate = parseRoute({ version: 1, current: input.candidate });
  if (native.engine === "tantivy" || candidate.engine !== "tantivy" || !["https://typeahead-tantivy-a.fly.dev", "https://typeahead-tantivy-b.fly.dev"].includes(candidate.origin)) {
    throw new Error("Live rehearsal requires an approved native origin and parallel Tantivy slot");
  }
  return { native, candidate };
}

export function liveOriginAdapter(configuration: ReturnType<typeof liveConfiguration>, upstream: (url: URL, init: RequestInit) => Promise<Response> = fetch) {
  const allowed = new Set<string>([configuration.native.origin, configuration.candidate.origin]);
  const started = performance.now();
  const calls: { origin: string; status: number; bytes: number; started_ms: number }[] = [];
  let count = 0, lastStarted = -1000;
  let pending = Promise.resolve();
  const request = async (input: Request) => {
    const url = new URL(input.url);
    if (input.method !== "GET" || !allowed.has(url.origin) || url.pathname !== "/search" || url.hash || url.username || url.password || [...url.searchParams.keys()].sort().join(",") !== "limit,q" || url.searchParams.get("q") !== "zzstoatzz.io" || url.searchParams.get("limit") !== "10") {
      throw new Error("Live rehearsal egress rejected");
    }
    if (++count > 16) throw new Error("Live rehearsal request budget exhausted");
    const result = pending.then(async () => {
      const wait = Math.max(0, 1000 - (performance.now() - lastStarted));
      if (wait) await Bun.sleep(wait);
      if (performance.now() - started > 60000) throw new Error("Live rehearsal wall budget exhausted");
      lastStarted = performance.now();
      const response = await upstream(url, { method: "GET", headers: { Accept: "application/json" }, credentials: "omit", redirect: "manual", signal: AbortSignal.timeout(2000) });
      if (response.status >= 300 && response.status < 400) { await response.body?.cancel(); throw new Error("Live rehearsal redirect rejected"); }
      const chunks: Uint8Array[] = [];
      let size = 0;
      const reader = response.body?.getReader();
      if (reader) try {
        for (;;) {
          const next = await reader.read();
          if (next.done) break;
          size += next.value.length;
          if (size > 1024 * 1024) { await reader.cancel(); throw new Error("Live rehearsal response budget exhausted"); }
          chunks.push(next.value);
        }
      } finally { reader.releaseLock(); }
      calls.push({ origin: url.origin, status: response.status, bytes: size, started_ms: lastStarted - started });
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      return new Response(bytes, { status: response.status, headers: response.headers });
    });
    pending = result.then(() => undefined, () => undefined);
    return result;
  };
  return { request, evidence: () => ({ request_count: count, maximum_requests: 16, minimum_interval_ms: 1000, maximum_response_bytes: 1048576, request_timeout_ms: 2000, maximum_wall_ms: 60000, calls: [...calls] }) };
}

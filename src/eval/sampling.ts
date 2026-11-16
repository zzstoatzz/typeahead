import { CORPUS_VERSION, ENDPOINTS, LIMIT, PROVIDERS, QUERIES, cooldown, responseSchema, type Observation, type Report } from "./model";

const HEADERS = ["server-timing", "cdn-cache", "age", "cache-control", "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "x-typeahead-engine", "x-typeahead-failover", "x-typeahead-build", "x-typeahead-binary-sha256", "cf-ray"];

export async function sample(o: Observation, fetcher: (url: string, init: RequestInit) => Promise<Response>): Promise<Observation> {
  const url = new URL(ENDPOINTS[o.provider]);
  url.search = new URLSearchParams({ q: o.q, limit: String(LIMIT) }).toString();
  const start = performance.now();
  let status: number | null = null;
  const headers: Record<string, string> = {};
  let cooldownUntil = 0;
  try {
    const response = await fetcher(url.href, {
      headers: { "User-Agent": "typeahead-eval/1 (+https://typeahead.waow.tech; contact: @zzstoatzz.io)", "X-Client": "bench" },
      signal: AbortSignal.timeout(10_000), redirect: "error",
    });
    status = response.status;
    for (const key of HEADERS) { const value = response.headers.get(key); if (value !== null) headers[key] = value; }
    cooldownUntil = cooldown(status, response.headers, Date.now());
    if (!response.ok) {
      await response.body?.cancel();
      return { ...o, status, headers, cooldownUntil, outcome: "http-error", error: `HTTP ${status}`, ms: performance.now() - start, finishedAt: Date.now() };
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("missing response body");
    const chunks: Uint8Array[] = [];
    let bytes = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      bytes += chunk.value.length;
      if (bytes > 1_048_576) { await reader.cancel(); throw new Error("response exceeds 1 MiB"); }
      chunks.push(chunk.value);
    }
    const ms = performance.now() - start;
    const body = await new Blob(chunks).text();
    try {
      const { actors } = responseSchema.parse(JSON.parse(body));
      return { ...o, status, headers, cooldownUntil, actors, outcome: "ok", ms, finishedAt: Date.now() };
    } catch {
      return { ...o, status, headers, cooldownUntil, outcome: "invalid-response", error: "invalid or duplicate actor results", ms, finishedAt: Date.now() };
    }
  } catch (error) {
    return { ...o, status, headers, cooldownUntil: Math.max(cooldownUntil, Date.now() + 300_000), outcome: "network-error", error: error instanceof Error ? error.message : String(error), ms: performance.now() - start, finishedAt: Date.now() };
  }
}

export function nextRunAt(lastStart: number | null): number {
  return lastStart === null ? 0 : lastStart + 86_400_000;
}

export function newRun(startedAt: number, location: string): Report {
  return { version: 1, run: new Date(startedAt).toISOString(), corpus: CORPUS_VERSION, location,
    startedAt, updatedAt: startedAt, expected: QUERIES.length * PROVIDERS.length * 2, queries: [...QUERIES], observations: [] };
}

export function reservation(run: Report, startedAt: number): Observation | undefined {
  const index = run.observations.length;
  const queries = run.queries ?? QUERIES;
  if (index >= run.expected) return;
  const round = Math.floor(index / (queries.length * PROVIDERS.length));
  const i = Math.floor(index / PROVIDERS.length) % queries.length;
  const provider = PROVIDERS[(round + i + index % PROVIDERS.length) % PROVIDERS.length];
  return { id: `${run.run}/${round}/${i}/${provider}`, run: run.run, corpus: run.corpus, provider,
    ...queries[i], round, startedAt, finishedAt: null, ms: null, outcome: "pending", status: null,
    actors: [], headers: {}, error: null, cooldownUntil: 0 };
}

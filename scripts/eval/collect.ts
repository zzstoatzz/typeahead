import { appendFile, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { hostname } from "node:os";
import { CORPUS_VERSION, ENDPOINTS, LIMIT, PROVIDERS, QUERIES, cooldown, nextAllowed, observationSchema, reportSchema, responseSchema, type Observation, type Report } from "../../src/eval/model";
import { ROOT, archiveReport, readRuns } from "./archive";

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
    const body = Buffer.concat(chunks).toString("utf8");
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

export async function collect({ scheduled = false } = {}) {
  await mkdir(ROOT, { recursive: true });
  const lock = join(ROOT, "collector.lock");
  await mkdir(lock).catch(() => { throw new Error(`collector already locked: ${lock}; inspect the owner before removing it`); });
  await writeFile(join(lock, "owner.json"), JSON.stringify({ pid: process.pid, host: hostname(), at: new Date().toISOString() }));
  const ledger = join(ROOT, "requests.jsonl");
  let report: Report | undefined;
  try {
    let runs = await readRuns();
    if (await Bun.file(join(ROOT, "latest.json")).exists()) {
      const interrupted = reportSchema.parse(JSON.parse(await readFile(join(ROOT, "latest.json"), "utf8")));
      if (!runs.some(r => r.run === interrupted.run)) {
        await archiveReport(interrupted);
        runs = await readRuns();
      }
    }
    const previous = await Bun.file(ledger).exists() ? await readFile(ledger, "utf8") : "";
    const indexed = new Map<string, Observation>();
    for (const line of previous.split("\n").filter(Boolean)) { const o = observationSchema.parse(JSON.parse(line)); indexed.set(o.id, o); }
    for (const run of runs) for (const o of run.observations) if (!indexed.has(o.id)) indexed.set(o.id, o);
    const lastStart = [...indexed.values()].reduce<number | null>((last, o) => Math.max(last ?? 0, Date.parse(o.run) || o.startedAt), runs[0]?.startedAt ?? null);
    if (scheduled && Date.now() < nextRunAt(lastStart)) { console.log(`not due until ${new Date(nextRunAt(lastStart)).toISOString()}`); return; }
    const start = Date.now();
    const run = new Date(start).toISOString();
    const current: Report = { version: 1, run, corpus: CORPUS_VERSION, location: process.env.EVAL_LOCATION ?? "local laptop", startedAt: start, updatedAt: start, expected: QUERIES.length * PROVIDERS.length * 2, queries: [...QUERIES], observations: [] };
    report = current;
    const save = async (o: Observation) => {
      await appendFile(ledger, JSON.stringify(o) + "\n", { flush: true });
      indexed.set(o.id, o);
      current.observations = [...indexed.values()].filter(value => value.run === run);
      current.updatedAt = Date.now();
      await writeFile(join(ROOT, "latest.tmp"), JSON.stringify(current, null, 2));
      await rename(join(ROOT, "latest.tmp"), join(ROOT, "latest.json"));
    };
    for (let round = 0; round < 2; round++) {
      for (const [i, query] of QUERIES.entries()) {
        const order = (round + i) % 2 ? [...PROVIDERS].reverse() : [...PROVIDERS];
        for (const provider of order) {
          const wait = nextAllowed([...indexed.values()], provider, Date.now()) - Date.now();
          if (wait > 30_000) { console.log(`stopping: ${provider} budget or cooldown until ${new Date(Date.now() + wait).toISOString()}`); return; }
          if (wait > 0) await Bun.sleep(wait);
          const o: Observation = { id: `${run}/${round}/${i}/${provider}`, run, corpus: CORPUS_VERSION, provider, ...query, round, startedAt: Date.now(), finishedAt: null, ms: null, outcome: "pending", status: null, actors: [], headers: {}, error: null, cooldownUntil: 0 };
          await save(o);
          const result = await sample(o, fetch);
          await save(result);
          console.log(`${current.observations.length}/${current.expected} ${provider} ${JSON.stringify(query.q)} ${result.outcome} ${result.ms?.toFixed(0)} ms`);
          if (result.cooldownUntil > Date.now()) { console.log("stopping after upstream backoff signal; no automatic retry"); return; }
        }
      }
    }
  } finally {
    try { if (report?.observations.length) await archiveReport(report); }
    finally { await rm(lock, { recursive: true }); }
  }
}

if (import.meta.main) await collect();

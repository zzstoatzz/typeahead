import { z } from "zod";

export const PROVIDERS = ["typeahead", "bluesky"] as const;
export type Provider = (typeof PROVIDERS)[number];
export const ENDPOINTS = {
  typeahead: "https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors",
  bluesky: "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead",
} satisfies Record<Provider, string>;
export const CORPUS_VERSION = "actor-search-v1";
export const LIMIT = 10;
export const GAP_MS = 5_000;
export const DAILY_BUDGET = 48;
export const QUERIES = [
  { q: "fig", category: "display name" },
  { q: "fig (", category: "punctuation" },
  { q: "fig aka", category: "multiword" },
  { q: "phil", category: "display name" },
  { q: "zzstoatzz", category: "handle prefix" },
  { q: "zzstoatzz.io", category: "exact handle" },
  { q: "nate", category: "common prefix" },
  { q: "alex", category: "common prefix" },
  { q: "bob", category: "common prefix" },
  { q: "aoc", category: "common prefix" },
  { q: "car", category: "common prefix" },
  { q: "Justin Fa", category: "multiword" },
  { q: "Justin Fag", category: "multiword" },
  { q: "paul frazee", category: "multiword" },
  { q: "jola.dev", category: "exact handle" },
  { q: "pfrazee.com", category: "exact handle" },
  { q: "André", category: "unicode" },
  { q: "東京", category: "unicode" },
  { q: "na", category: "short prefix" },
  { q: "a", category: "short prefix" },
] as const;

export const actorSchema = z.object({
  did: z.string().startsWith("did:").max(2048),
  handle: z.string().min(1).max(2048),
  displayName: z.string().max(4096).optional(),
  avatar: z.string().max(4096).optional(),
});
export const responseSchema = z.object({ actors: z.array(actorSchema).max(LIMIT) })
  .refine(({ actors }) => new Set(actors.map(a => a.did)).size === actors.length, "duplicate DID");
export const observationSchema = z.object({
  id: z.string(), run: z.string(), corpus: z.string(), provider: z.enum(PROVIDERS),
  q: z.string(), category: z.string(), round: z.number().int(),
  startedAt: z.number(), finishedAt: z.number().nullable(), ms: z.number().nonnegative().nullable(),
  outcome: z.enum(["pending", "ok", "http-error", "invalid-response", "network-error"]),
  status: z.number().int().nullable(), actors: z.array(actorSchema),
  headers: z.record(z.string(), z.string()), error: z.string().nullable(), cooldownUntil: z.number(),
});
export type Observation = z.infer<typeof observationSchema>;
export type AttemptTiming = Pick<Observation, "provider" | "startedAt" | "finishedAt" | "cooldownUntil">;
export const reportSchema = z.object({
  version: z.literal(1), run: z.string(), corpus: z.string(), location: z.string(),
  startedAt: z.number(), updatedAt: z.number(), expected: z.number().int().positive(),
  queries: z.array(z.object({ q: z.string(), category: z.string() })).optional(),
  observations: z.array(observationSchema),
}).superRefine((report, ctx) => {
  const seen = new Set<string>();
  for (const o of report.observations) {
    const key = JSON.stringify([o.provider, o.q, o.round]);
    if (seen.has(key) || o.run !== report.run || o.corpus !== report.corpus)
      ctx.addIssue({ code: "custom", message: "duplicate or mismatched observation" });
    seen.add(key);
    if (o.outcome === "ok" && (o.ms === null || o.finishedAt === null || o.status === null || o.status < 200 || o.status >= 300))
      ctx.addIssue({ code: "custom", message: "successful observation is incomplete" });
  }
  if (report.observations.length > report.expected) ctx.addIssue({ code: "custom", message: "more observations than planned" });
});
export type Report = z.infer<typeof reportSchema>;

export function fallback(o: Observation): boolean {
  return o.provider === "typeahead" && (o.headers["x-typeahead-engine"] === "bluesky" || !!o.headers["x-typeahead-failover"]);
}

export function cacheState(o: Observation): "hit" | "miss" | "unknown" {
  if (/cache;desc="hit"/i.test(o.headers["server-timing"] ?? "") || o.headers["cdn-cache"]?.toUpperCase() === "HIT") return "hit";
  if (/cache;desc="miss"/i.test(o.headers["server-timing"] ?? "") || o.headers["cdn-cache"]?.toUpperCase() === "MISS") return "miss";
  return "unknown";
}

export function mean(values: number[]): number | null {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

export function percentile(values: number[], fraction: number): number | null {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] : null;
}

export function summarize(observations: Observation[]) {
  const completed = observations.filter(o => o.outcome !== "pending");
  const ok = completed.filter(o => o.outcome === "ok");
  const latencies = ok.flatMap(o => o.ms === null ? [] : [o.ms]);
  const actors = ok.flatMap(o => o.actors);
  const exact = ok.filter(o => o.category === "exact handle");
  return {
    attempts: observations.length, completed: completed.length, ok: ok.length,
    failures: completed.length - ok.length, pending: observations.length - completed.length,
    averageMs: mean(latencies), medianMs: percentile(latencies, .5), p90Ms: percentile(latencies, .9),
    successPct: completed.length ? 100 * ok.length / completed.length : null,
    emptyPct: ok.length ? 100 * ok.filter(o => !o.actors.length).length / ok.length : null,
    results: mean(ok.map(o => o.actors.length)),
    displayPct: actors.length ? 100 * actors.filter(a => a.displayName?.trim()).length / actors.length : null,
    avatarPct: actors.length ? 100 * actors.filter(a => a.avatar).length / actors.length : null,
    exactPct: exact.length ? 100 * exact.filter(o => o.actors[0]?.handle.toLowerCase() === o.q.toLowerCase()).length / exact.length : null,
    exactCount: exact.length, actorCount: actors.length,
    hits: completed.filter(o => cacheState(o) === "hit").length,
    misses: completed.filter(o => cacheState(o) === "miss").length,
    unknownCache: completed.filter(o => cacheState(o) === "unknown").length,
    fallbacks: completed.filter(fallback).length,
  };
}

export function pairs(observations: Observation[]) {
  const by = new Map<string, Partial<Record<Provider, Observation>>>();
  for (const o of observations) {
    const key = JSON.stringify([o.run, o.corpus, o.round, o.q]);
    const pair = by.get(key) ?? {};
    pair[o.provider] = o;
    by.set(key, pair);
  }
  return [...by.values()].flatMap(pair => {
    const a = pair.typeahead, b = pair.bluesky;
    if (!a || !b || a.outcome !== "ok" || b.outcome !== "ok" || fallback(a)) return [];
    const ours = new Set(a.actors.map(actor => actor.did));
    const theirs = new Set(b.actors.map(actor => actor.did));
    const shared = [...ours].filter(did => theirs.has(did)).length;
    const union = new Set([...ours, ...theirs]).size;
    return [{ a, b, shared, overlap: union ? 100 * shared / union : null,
      sameFirst: !!a.actors[0] && a.actors[0].did === b.actors[0]?.did }];
  });
}

export function nextAllowed(observations: readonly AttemptTiming[], provider: Provider, now: number): number {
  const recent = observations.filter(o => o.provider === provider && o.startedAt > now - 86_400_000)
    .sort((a, b) => a.startedAt - b.startedAt);
  const budget = recent.length >= DAILY_BUDGET ? recent[recent.length - DAILY_BUDGET].startedAt + 86_400_000 : 0;
  return observations.reduce((next, o) => Math.max(next, o.provider === provider ? o.cooldownUntil : 0,
    Math.max(o.startedAt, o.finishedAt ?? o.startedAt) + GAP_MS), budget);
}

export function cooldown(status: number, headers: Headers, now: number): number {
  const retry = headers.get("retry-after");
  const delay = retry && /^\d+$/.test(retry) ? now + Number(retry) * 1000 : retry ? Date.parse(retry) : 0;
  const reset = Number(headers.get("ratelimit-reset")) * 1000;
  const exhausted = headers.get("ratelimit-remaining") === "0";
  const floor = status === 429 || exhausted ? now + 3_600_000 : status >= 500 ? now + 300_000 : 0;
  return Math.max(floor, Number.isFinite(delay) ? delay : 0, exhausted || status === 429 ? reset || 0 : 0);
}

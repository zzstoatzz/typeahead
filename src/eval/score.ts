import { reportProviders, fallback, mean, type Observation, type Provider } from "./model";

export const SCORE_VERSION = "service-v1";
export const DIMENSIONS = [
  { id: "speed", label: "response speed", weight: 40, rule: "100 at ≤100 ms; 0 at ≥1,000 ms; logarithmic between. Failed requests score 0." },
  { id: "availability", label: "successful answers", weight: 40, rule: "100 for a valid response, including an empty answer; 0 for a failed request." },
  { id: "exact", label: "exact-handle rank", weight: 20, rule: "100 when the requested exact handle is first; 0 otherwise. Only exact-handle cases." },
] as const;
export type Dimension = (typeof DIMENSIONS)[number]["id"];
export type Weights = Record<Dimension, number>;
export const DEFAULT_WEIGHTS = { speed: 40, availability: 40, exact: 20 } satisfies Weights;

export function weightsFrom(params: URLSearchParams) {
  const value = (id: Dimension) => {
    const raw = params.get(id);
    const n = raw === null || raw.trim() === "" ? DEFAULT_WEIGHTS[id] : Number(raw);
    return Number.isFinite(n) && n >= 0 && n <= 100 ? n : DEFAULT_WEIGHTS[id];
  };
  return { speed: value("speed"), availability: value("availability"), exact: value("exact") };
}

function value(o: Observation, dimension: Dimension): number | null {
  if (dimension === "exact" && o.category !== "exact handle") return null;
  if (o.outcome !== "ok") return 0;
  switch (dimension) {
    case "availability": return 100;
    case "exact": return o.actors[0]?.handle.toLowerCase() === o.q.toLowerCase() ? 100 : 0;
    case "speed": return o.ms === null ? null : Math.min(100, Math.max(0, 100 * Math.log10(1000 / Math.max(o.ms, .001))));
  }
}

export function score(observations: Observation[], weights: Weights = DEFAULT_WEIGHTS, selectedProviders = reportProviders({ observations })) {
  const grouped = new Map<string, Partial<Record<Provider, Observation>>>();
  for (const o of observations) {
    const key = JSON.stringify([o.run, o.corpus, o.q, o.round]);
    grouped.set(key, { ...grouped.get(key), [o.provider]: o });
  }
  const matched = [...grouped.values()].flatMap(p => {
    const rows = selectedProviders.flatMap(provider => p[provider] ? [p[provider]] : []);
    return rows.length === selectedProviders.length && rows.every(o => o.outcome !== "pending" && !fallback(o)) ? rows : [];
  });
  const totalWeight = DIMENSIONS.reduce((s, d) => s + weights[d.id], 0);
  const providers = selectedProviders.map(provider => {
    const rows = matched.filter(o => o.provider === provider);
    const dimensions = DIMENSIONS.map(d => {
      const byQuery = new Map<string, number[]>();
      for (const o of rows) {
        const v = value(o, d.id);
        if (v !== null) byQuery.set(o.q, [...(byQuery.get(o.q) ?? []), v]);
      }
      const points = mean([...byQuery.values()].flatMap(values => { const v = mean(values); return v === null ? [] : [v]; }));
      return { ...d, points, queries: byQuery.size, weight: weights[d.id], contribution: points === null || !totalWeight ? null : points * weights[d.id] / totalWeight };
    });
    const ready = totalWeight > 0 && dimensions.every(d => d.weight === 0 || d.points !== null);
    return { provider, dimensions, total: ready ? dimensions.reduce((sum, d) => sum + (d.contribution ?? 0), 0) : null };
  });
  return { providers, matched: matched.length / selectedProviders.length, excluded: grouped.size - matched.length / selectedProviders.length, queries: new Set(matched.map(o => o.q)).size };
}

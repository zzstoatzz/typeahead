import evidence from "../../data/eval/timing-evidence/2026-11-16T07-25-35.153Z.json";
import { timingDurations } from "../server-timing";
import { cacheState, fallback, mean, type Observation } from "./model";

export function observationTiming(o: Observation) {
  const values = timingDurations(o.headers["server-timing"]);
  const logged = o.run === evidence.run ? evidence.observations.find(e => e.id === o.id && e.cfRay === o.headers["cf-ray"] && e.ms === o.ms) : undefined;
  if (logged) {
    if (!values.has("transport_route")) values.set("transport_route", logged.routeMs);
    if (!values.has("transport_origin_headers")) values.set("transport_origin_headers", logged.originMs);
  }
  const total = o.ms ?? 0;
  const worker = values.get("worker"), cache = values.get("cache") ?? 0;
  const hit = cacheState(o) === "hit";
  const origin = hit ? 0 : values.get("origin") ?? 0;
  const valid = worker !== undefined && worker <= total + 1 && cache + origin <= worker + 1;
  const boundedWorker = valid ? Math.min(worker, total) : 0;
  const boundedCache = valid ? Math.min(cache, boundedWorker) : 0;
  const boundedOrigin = valid ? Math.min(origin, boundedWorker - boundedCache) : 0;
  return {
    observation: o, values, logged: !!logged, total,
    outside: valid ? total - boundedWorker : 0,
    cache: boundedCache, backend: boundedOrigin,
    workerOther: valid ? boundedWorker - boundedCache - boundedOrigin : 0,
    unknown: valid ? 0 : total,
  };
}

export function timingSummary(observations: Observation[]) {
  const samples = observations.filter(o => o.provider === "typeahead" && o.outcome === "ok" && o.ms !== null && !fallback(o)).map(observationTiming);
  const components = [
    { key: "outside", label: "outside Worker", description: "Client, connections, network and response transfer. Elapsed time minus the public Worker timer." },
    { key: "cache", label: "cache lookup", description: "Cache lookup and initial search handling in the public Worker." },
    { key: "backend", label: "backend round trip", description: "Public Worker → transport → Fly → response. Includes routing and search, not just the engine." },
    { key: "workerOther", label: "other Worker time", description: "Worker duration not covered by cache lookup or the backend round trip." },
    { key: "unknown", label: "unattributed", description: "Missing or inconsistent Worker timing; no stage is assumed to be zero." },
  ] as const;
  return {
    samples, total: mean(samples.map(s => s.total)),
    components: components.map(c => ({ ...c, ms: mean(samples.map(s => s[c.key])) })),
    inside: [
      { key: "transport_route", label: "route validation" },
      { key: "transport_origin_headers", label: "transport → Fly → headers" },
      { key: "engine", label: "engine handler" },
      { key: "transport_total", label: "validation + fetch elapsed" },
    ].map(c => {
      const measured = samples.flatMap(s => {
        if (cacheState(s.observation) === "hit") return [];
        const value = s.values.get(c.key);
        return value === undefined ? [] : [value];
      });
      return { ...c, n: measured.length, ms: mean(measured) };
    }),
  };
}

import { expect, test } from "bun:test";
import { observationTiming, timingSummary } from "./timing";
import { reportSchema, type Observation } from "./model";
import seed from "../../data/eval/runs/2026-11-16T07-25-35.153Z.json";

const report = reportSchema.parse(seed);
const original = report.observations[0];
const sample = (header: string, ms = 100): Observation => ({ ...original, run: "fixture", id: "fixture", ms, headers: { "server-timing": header } });

test("archived evidence joins only exact observations and preserves the original report", () => {
  const before = JSON.stringify(report);
  const result = timingSummary(report.observations);
  expect(result.samples.filter(s => s.logged).length).toBe(40);
  expect(result.inside.find(c => c.key === "transport_route")?.ms).toBe(17.775);
  expect(result.inside.find(c => c.key === "transport_origin_headers")?.ms).toBe(20.925);
  expect(result.inside.find(c => c.key === "engine")?.ms).toBeNull();
  expect(observationTiming({ ...original, ms: 1 }).logged).toBe(false);
  expect(observationTiming({ ...original, headers: {} }).logged).toBe(false);
  expect(JSON.stringify(report)).toBe(before);
});

test("missing timings are unattributed, caches don't inherit backend costs, and fallbacks are excluded", () => {
  expect(observationTiming(sample("")).unknown).toBe(100);
  expect(observationTiming(sample("worker;dur=200")).unknown).toBe(100);
  const hit = observationTiming(sample('worker;dur=8, cache;desc="hit";dur=6, origin;dur=90'));
  expect(hit.backend).toBe(0);
  expect(hit.workerOther).toBe(2);
  const result = timingSummary([sample("engine;dur=2"), sample(""), { ...sample(""), headers: { "x-typeahead-failover": "automatic" } }]);
  expect(result.samples.length).toBe(2);
  expect(result.inside.find(c => c.key === "engine")).toMatchObject({ ms: 2, n: 1 });
});

test("replayable duration partitions conserve elapsed time without double counting nested spans", () => {
  let seed = 16112026;
  const random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 2 ** 32; };
  for (let i = 0; i < 1000; i++) {
    const total = random() * 1000, worker = random() * total, cache = random() * worker, origin = random() * (worker - cache);
    const s = observationTiming(sample(`worker;dur=${worker}, cache;dur=${cache}, origin;dur=${origin}, engine;dur=2, transport_route;dur=20`, total));
    expect(s.outside + s.cache + s.backend + s.workerOther + s.unknown).toBeCloseTo(total, 8);
    expect([s.outside, s.cache, s.backend, s.workerOther, s.unknown].every(v => v >= 0)).toBe(true);
  }
});

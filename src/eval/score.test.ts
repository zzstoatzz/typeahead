import { expect, test } from "bun:test";
import { score, weightsFrom } from "./score";
import type { Observation } from "./model";

const a: Observation = { id: "a", run: "r", corpus: "v1", provider: "typeahead", q: "alice.test", category: "exact handle", round: 0,
  startedAt: 0, finishedAt: 100, ms: 100, outcome: "ok", status: 200, actors: [{ did: "did:plc:alice", handle: "alice.test" }], headers: {}, error: null, cooldownUntil: 0 };
const b: Observation = { ...a, id: "b", provider: "bluesky", ms: 1000 };

test("fixed score anchors and changed weights are reproducible without new data", () => {
  expect(score([a, b]).providers.map(p => p.total)).toEqual([100, 60]);
  expect(score([a, b], { speed: 0, availability: 0, exact: 100 }).providers.map(p => p.total)).toEqual([100, 100]);
  expect(score([a, b], { speed: 0, availability: 0, exact: 0 }).providers.map(p => p.total)).toEqual([null, null]);
  expect(weightsFrom(new URLSearchParams({ speed: "NaN", availability: "-1", exact: "0" }))).toEqual({ speed: 40, availability: 40, exact: 0 });
});

test("failures score zero instead of improving latency through exclusion", () => {
  expect(score([{ ...a, outcome: "network-error", actors: [], ms: 1 }, b]).providers.map(p => p.total)).toEqual([0, 60]);
  expect(score([{ ...a, outcome: "pending" }, b])).toMatchObject({ matched: 0, excluded: 1 });
  expect(score([{ ...a, headers: { "x-typeahead-engine": "bluesky" } }, b])).toMatchObject({ matched: 0, excluded: 1 });
});

test("repeats don't overweight a query; missing exact checks don't silently redistribute weight", () => {
  const rows = [a, b, { ...a, round: 1 }, { ...b, round: 1 }, { ...a, q: "bob", category: "prefix", ms: 1000 }, { ...b, q: "bob", category: "prefix" }];
  expect(score(rows).providers[0].total).toBe(80);
  expect(score(rows.slice(-2)).providers[0].total).toBeNull();
  expect(score(rows.slice(-2), { speed: 1, availability: 1, exact: 0 }).providers[0].total).toBe(50);
});

test("three-provider scores use the same completed cases and historical pairs still score", () => {
  const tern: Observation = { ...a, id: "t", provider: "tern", ms: 100 };
  const providers = ["typeahead", "bluesky", "tern"] as const;
  expect(score([a, b, tern], undefined, [...providers]).providers.map(p => p.total)).toEqual([100, 60, 100]);
  expect(score([a, b], undefined, [...providers])).toMatchObject({ matched: 0, excluded: 1 });
  expect(score([a, b]).providers.map(p => p.total)).toEqual([100, 60]);
});

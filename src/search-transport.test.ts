import { expect, test } from "bun:test";
import { allowedRoute, parseRoute, routeIdentity, responseMatchesRoute } from "./search-route";

test("routing records accept only managed Tantivy targets and ignore retired envelope fields", () => {
  const current = { engine: "tantivy", origin: "https://typeahead-tantivy-b.fly.dev", build_id: "b1793463173-cp-fixture", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "global-merge" } as const;
  const retired = { origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1793109259-2b36-px05", source_watermark: 1793109254, machine_id: "8e3d79b70dd748", volume_id: "vol_v8eyqk61q9dy9gkv", legacy_build_id: "b1793109259-2b36" };
  expect(parseRoute({ version: 1, current })).toEqual(current);
  expect(parseRoute({ version: 1, current: { ...current, extra: true }, previous: current })).toEqual(current);
  expect(parseRoute({ version: 1, current, previous: { ...current, origin: "https://typeahead-tantivy-a.fly.dev" }, native_rollback: retired })).toEqual(current);
  for (const value of [undefined, null, false, 1, "route", [], {}, { version: 1 }, { version: 2, current },
    { version: 1, current: null },
    { version: 1, current: [] },
    { version: 1, current: "target" },
    { version: 1, current: retired },
    { version: 1, current: { ...retired, engine: "native" } },
    { version: 1, current: retired, previous: current, native_rollback: retired },
    { version: 1, current: { build_id: current.build_id } },
    { version: 1, current: { origin: current.origin } },
    { version: 1, current: { ...current, origin: 1 } },
    { version: 1, current: { ...current, build_id: 1 } },
    { version: 1, current: { ...current, origin: "https://example.com" } },
    { version: 1, current: { ...current, origin: current.origin + "/" } },
    { version: 1, current: { ...current, build_id: "../../other" } }]) {
    expect(() => parseRoute(value)).toThrow();
  }
});

test("Tantivy requires explicit engine, bounded origin, source format and admission identity", () => {
  const candidate = { engine: "tantivy", origin: "https://typeahead-tantivy-dev.fly.dev", build_id: "b1792908655-test", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "per-segment" } as const;
  const route = parseRoute({ version: 1, current: candidate });
  expect(route).toEqual(candidate);
  for (const origin of ["https://typeahead-tantivy-a.fly.dev", "https://typeahead-tantivy-b.fly.dev"] as const) {
    expect(parseRoute({ version: 1, current: { ...candidate, origin } }).origin).toBe(origin);
  }
  for (const changed of [{ engine: undefined }, { engine: "native" }, { origin: "https://typeahead-startup-20261024.fly.dev" }, { origin: "https://other.fly.dev" }, { format: "tantivy-0.27-f32-did-v1" }, { admission_sha256: "invalid" }, { binary_sha256: "invalid" }, { retrieval_strategy: "unknown" }]) expect(() => parseRoute({ version: 1, current: { ...candidate, ...changed } })).toThrow();
  expect(routeIdentity(route)).not.toBe(routeIdentity(parseRoute({ version: 1, current: { ...candidate, admission_sha256: "b".repeat(64) } })));
  expect(responseMatchesRoute(route, new Response("{}"))).toBe(false);
  expect(responseMatchesRoute(route, new Response("{}", { headers: { "X-Typeahead-Engine": "tantivy", "X-Typeahead-Build": candidate.build_id, "X-Typeahead-Format": candidate.format, "X-Typeahead-Binary-Sha256": candidate.binary_sha256, "X-Typeahead-Retrieval-Strategy": candidate.retrieval_strategy } }))).toBe(true);
  expect(responseMatchesRoute(route, new Response("{}", { status: 503 }))).toBe(true);
});

test("the store slot is a routable target with its own acknowledged identity", () => {
  const managed = { engine: "tantivy", origin: "https://typeahead-tantivy-b.fly.dev", build_id: "b1794377098-cp-managed", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "global-merge" } as const;
  const store = { ...managed, origin: "https://typeahead-shadow-a.fly.dev", build_id: "b1794333857-cp-store", admission_sha256: "b".repeat(64), binary_sha256: "d".repeat(64) } as const;
  const route = parseRoute({ version: 1, current: store, previous: managed });
  expect(route).toEqual(store);
  expect(routeIdentity(route)).not.toBe(routeIdentity(parseRoute({ version: 1, current: managed })));
  const acknowledged = (target: typeof managed | typeof store) => new Response("{}", { headers: { "X-Typeahead-Engine": "tantivy", "X-Typeahead-Build": target.build_id, "X-Typeahead-Format": target.format, "X-Typeahead-Binary-Sha256": target.binary_sha256, "X-Typeahead-Retrieval-Strategy": target.retrieval_strategy } });
  expect(responseMatchesRoute(route, acknowledged(store))).toBe(true);
  expect(responseMatchesRoute(route, acknowledged(managed))).toBe(false);
  for (const origin of ["https://typeahead-shadow-c.fly.dev", "https://typeahead-shadow-a.fly.dev/", "http://typeahead-shadow-a.fly.dev", "https://typeahead-shadow-b.fly.dev/", "http://typeahead-shadow-b.fly.dev"]) {
    expect(() => parseRoute({ version: 1, current: { ...store, origin } })).toThrow();
  }
});

test("either store slot is routable, and the record's previous never changes the route", () => {
  const slotA = { engine: "tantivy", origin: "https://typeahead-shadow-a.fly.dev", build_id: "b1794333857-cp-store-a", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "global-merge" } as const;
  const slotB = { ...slotA, origin: "https://typeahead-shadow-b.fly.dev", build_id: "b1794450000-cp-store-b", admission_sha256: "b".repeat(64) } as const;
  const managed = { ...slotA, origin: "https://typeahead-tantivy-b.fly.dev", build_id: "b1794377098-cp-managed", admission_sha256: "d".repeat(64), binary_sha256: "e".repeat(64) } as const;
  expect(parseRoute({ version: 1, current: slotB, previous: slotA })).toEqual(slotB);
  const serving = parseRoute({ version: 1, current: slotA, previous: managed });
  expect(serving).toEqual(slotA);
  expect(routeIdentity(parseRoute({ version: 1, current: slotA, previous: slotB }))).toBe(routeIdentity(serving));
  expect(routeIdentity(parseRoute({ version: 1, current: slotA }))).toBe(routeIdentity(serving));
  expect(routeIdentity(parseRoute({ version: 1, current: slotB, previous: slotA }))).not.toBe(routeIdentity(serving));
});

test("router exposes only backend read paths", () => {
  const base = "https://typeahead-search-transport.n8-3e9.workers.dev";
  for (const path of ["/search", "/ready", "/health", "/index/status", "/debug/search", "/route"]) {
    expect(allowedRoute(new Request(base + path))).toBe(true);
  }
  for (const [path, method] of [["/admin/refresh", "POST"], ["/admin/refresh", "GET"], ["/search", "POST"], ["/admin/delete", "POST"], ["/debug/index", "GET"], ["/other", "GET"]]) {
    expect(allowedRoute(new Request(base + path, { method }))).toBe(false);
  }
});

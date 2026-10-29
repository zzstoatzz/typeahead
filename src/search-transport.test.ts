import { expect, test } from "bun:test";
import { allowedRoute, parseRoute, routeIdentity, responseMatchesRoute } from "./search-route";

test("routing records accept only managed backend origins and exact build identities", () => {
  const current = { origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1792896197-ebe5-px05" } as const;
  expect(parseRoute({ version: 1, current })).toEqual(current);
  expect(parseRoute({ version: 1, current: { ...current, origin: "https://typeahead-search-refresh.fly.dev" } }))
    .toEqual({ ...current, origin: "https://typeahead-search-refresh.fly.dev" });
  expect(parseRoute({ version: 1, current: { ...current, extra: true }, previous: current })).toEqual(current);
  for (const value of [undefined, null, false, 1, "route", [], {}, { version: 1 }, { version: 2, current },
    { version: 1, current: null },
    { version: 1, current: [] },
    { version: 1, current: "target" },
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

test("router exposes only existing backend read paths and authenticated refresh path", () => {
  const base = "https://typeahead-search-transport.n8-3e9.workers.dev";
  for (const path of ["/search", "/ready", "/health", "/index/status", "/debug/search", "/debug/index", "/route"]) {
    expect(allowedRoute(new Request(base + path))).toBe(true);
  }
  expect(allowedRoute(new Request(base + "/admin/refresh", { method: "POST" }))).toBe(true);
  for (const [path, method] of [["/admin/refresh", "GET"], ["/search", "POST"], ["/admin/delete", "POST"], ["/other", "GET"]]) {
    expect(allowedRoute(new Request(base + path, { method }))).toBe(false);
  }
});

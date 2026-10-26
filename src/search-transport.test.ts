import { expect, test } from "bun:test";
import { allowedRoute, parseRoute } from "./search-route";

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

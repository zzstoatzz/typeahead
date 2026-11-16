import { expect, test } from "bun:test";
import { retainedBackendTiming, timingDurations } from "./server-timing";

test("timing parser ignores quoted delimiters, descriptions and ambiguous durations", () => {
  const header = 'cache;desc="miss, dur=99; \\"quoted\\"";dur=6, search;dur=2.319, engine;dur=NaN, transport_route;dur=12;dur=13';
  expect(timingDurations(header).get("cache")).toBe(6);
  expect(timingDurations(header).get("search")).toBe(2.319);
  expect(timingDurations(header).has("engine")).toBe(false);
  expect(timingDurations(header).has("transport_route")).toBe(false);
  for (const value of ["-1", "Infinity", "1e5", "60001", '"2"', "2oops"]) expect(timingDurations(`engine;dur=${value}`).size).toBe(0);
  expect(timingDurations("engine;dur=1, engine;dur=2").size).toBe(0);
  expect(timingDurations("x".repeat(8193)).size).toBe(0);
});

test("only named numeric backend intervals cross the public boundary", () => {
  expect(retainedBackendTiming('search;dur=8, engine;dur=2.5, secret;desc="private", transport_total;dur=35')).toBe("engine;dur=2.5, transport_total;dur=35");
});

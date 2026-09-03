import { describe, test, expect } from "bun:test";
import { applyLabelHistory, percentileFromQuantiles, LABELERS } from "./labelers";

describe("applyLabelHistory", () => {
  test("a negation withdraws the earlier label of the same val", () => {
    const out = applyLabelHistory([
      { val: "spam", uri: "did:plc:x", src: "did:plc:l", cts: "2026-01-01T00:00:00Z" },
      { val: "spam", uri: "did:plc:x", src: "did:plc:l", cts: "2026-02-01T00:00:00Z", neg: true },
      { val: "!warn", uri: "did:plc:x", src: "did:plc:l", cts: "2026-03-01T00:00:00Z" },
    ]);
    expect(out.map((l) => l.val)).toEqual(["!warn"]);
  });

  test("order in the response does not matter, the latest record wins", () => {
    const out = applyLabelHistory([
      { val: "spam", uri: "u", src: "s", cts: "2026-02-01T00:00:00Z", neg: true },
      { val: "spam", uri: "u", src: "s", cts: "2026-03-01T00:00:00Z" },
    ]);
    expect(out.map((l) => l.val)).toEqual(["spam"]);
  });

  test("an expired label is not applied", () => {
    const now = new Date("2026-06-01T00:00:00Z");
    const out = applyLabelHistory([
      { val: "temp", uri: "u", src: "s", cts: "2026-01-01T00:00:00Z", exp: "2026-02-01T00:00:00Z" },
      { val: "keep", uri: "u", src: "s", cts: "2026-01-01T00:00:00Z", exp: "2027-01-01T00:00:00Z" },
    ], now);
    expect(out.map((l) => l.val)).toEqual(["keep"]);
  });
});

describe("percentileFromQuantiles", () => {
  const q = Array.from({ length: 101 }, (_, i) => i * 10); // p_i = 10i
  test("finds the band a score falls in", () => {
    expect(percentileFromQuantiles(0.5, q)).toBe(0);
    expect(percentileFromQuantiles(250, q)).toBe(25);
    expect(percentileFromQuantiles(255, q)).toBe(25);
    expect(percentileFromQuantiles(1000, q)).toBe(100);
    expect(percentileFromQuantiles(99999, q)).toBe(100);
  });
  test("no quantiles or no positive score means no percentile", () => {
    expect(percentileFromQuantiles(5, [])).toBeNull();
    expect(percentileFromQuantiles(0, q)).toBeNull();
  });
});

describe("labelers.json", () => {
  test("every entry has a resolvable endpoint and the appview operators are pinned", () => {
    for (const l of LABELERS) {
      expect(l.did.startsWith("did:")).toBe(true);
      expect(l.endpoint.startsWith("https://")).toBe(true);
      expect(l.endpoint.endsWith("/")).toBe(false);
    }
    const handles = LABELERS.map((l) => l.handle);
    for (const h of ["moderation.bsky.app", "moderation.blacksky.app", "moderation.northsky.social", "ozone.eurosky.social"]) {
      expect(handles).toContain(h);
    }
  });
});

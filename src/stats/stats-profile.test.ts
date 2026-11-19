import { expect, test } from "bun:test";
import { hourOfDayBands, parseProfile, quantile } from "./stats-profile";

test("quantile interpolates between closest ranks", () => {
  expect(quantile([0, 10, 20, 30, 40], 0.5)).toBe(20);
  expect(quantile([0, 10], 0.25)).toBe(2.5);
  expect(quantile([7], 0.9)).toBe(7);
});

test("bands are per UTC hour of day, counting missing buckets as zero", () => {
  // two days of 5-minute buckets starting at midnight UTC
  const start = 20_000 * 288;
  const end = start + 2 * 288;
  const totals = new Map<number, number>();
  for (let b = start; b < end; b++) if (Math.floor(b / 12) % 24 === 14) totals.set(b, 10);
  const m5 = hourOfDayBands(totals, start, end, "m5");
  expect(m5[14]).toEqual({ p10: 10, p25: 10, p50: 10, p75: 10, p90: 10 });
  expect(m5[3]).toEqual({ p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 });
  const h = hourOfDayBands(totals, start, end, "h");
  expect(h[14]!.p50).toBe(120);
  expect(h.length).toBe(24);
});

test("a stored profile of the wrong shape is ignored", () => {
  expect(parseProfile(null)).toBeNull();
  expect(parseProfile("{}")).toBeNull();
  expect(parseProfile("not json")).toBeNull();
});

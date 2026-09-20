import { test, expect } from "bun:test";
import { compact, overview, overviewFigures } from "./og";
import type { StatsKvData } from "../types";

test("compact: millions to one decimal, thousands rounded, small numbers exact", () => {
  expect(compact(11_901_310)).toBe("11.9M");
  expect(compact(12_000_000)).toBe("12M");
  expect(compact(8_224_000)).toBe("8.2M");
  expect(compact(48_500)).toBe("49K");
  expect(compact(10_000)).toBe("10K");
  expect(compact(2_310)).toBe("2,310");
  expect(compact(0)).toBe("0");
});

const nowBucket = Math.floor(Date.now() / 300_000);
const kv: StatsKvData = {
  metrics: [
    { hour: nowBucket, searches: 10, total_ms: 0, cache_hits: 5, cache_ms: 0 },
    { hour: nowBucket - 3 * 288, searches: 20, total_ms: 0, cache_hits: 0, cache_ms: 0 },
    // eight days old: outside the card's window
    { hour: nowBucket - 8 * 288, searches: 1000, total_ms: 0, cache_hits: 1000, cache_ms: 0 },
  ],
  snapshots: [
    { hour: 1, total: 1, with_handles: 1, with_avatars: 0, hidden: 0 },
    { hour: 2, total: 11_901_310, with_handles: 11_901_310, with_avatars: 8_224_000, hidden: 2_310 },
  ],
  deltas: [],
  materialized_at: 0,
};

test("overviewFigures: latest snapshot, searches summed over the last seven days only", () => {
  expect(overviewFigures(kv)).toEqual({
    total: 11_901_310, withHandles: 11_901_310, withAvatars: 8_224_000, hidden: 2_310, searches7d: 35,
  });
  expect(overviewFigures(null)).toEqual({ total: 0, withHandles: 0, withAvatars: 0, hidden: 0, searches7d: 0 });
});

test("the default card carries the compact figures and does not define itself by bluesky", () => {
  const html = overview(overviewFigures(kv));
  expect(html).toContain("11.9M");
  expect(html).toContain("actors indexed");
  expect(html).toContain("69.1%");
  expect(html).toContain("with avatars");
  expect(html).toContain("2,310");
  expect(html).toContain("moderation labels");
  expect(html).toContain("100% with handles");
  expect(html).toContain("last 7 days");
  expect(html.toLowerCase()).not.toContain("bluesky");
});

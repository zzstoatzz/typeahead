import { test, expect } from "bun:test";
import { compact, firstCard, firstTiles, overview, overviewFigures } from "./og";
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
    { hour: nowBucket - 100 * 288, searches: 1000, total_ms: 0, cache_hits: 1000, cache_ms: 0 },
  ],
  snapshots: [
    { hour: 1, total: 1, with_handles: 1, with_avatars: 0, hidden: 0 },
    { hour: 2, total: 11_901_310, with_handles: 11_901_310, with_avatars: 8_224_000, hidden: 2_310 },
  ],
  deltas: [],
  traffic_sources_total: 219,
  materialized_at: 0,
};

test("overviewFigures: latest snapshot, every search ever, every app ever", () => {
  expect(overviewFigures(kv)).toEqual({
    total: 11_901_310, withHandles: 11_901_310, withAvatars: 8_224_000, apps: 219, searches: 2_035,
  });
  expect(overviewFigures(null)).toEqual({ total: 0, withHandles: 0, withAvatars: 0, apps: 0, searches: 0 });
  // before the count is materialized, distinct app domains in the daily series stand in
  const { traffic_sources_total: _, ...older } = kv;
  const daily = [
    { day: 1, domain: "plyr.fm", hits: 5 }, { day: 2, domain: "plyr.fm", hits: 5 },
    { day: 2, domain: "haunt.app", hits: 1 }, { day: 2, domain: "unknown", hits: 9 }, { day: 2, domain: "homepage", hits: 9 },
  ];
  expect(overviewFigures({ ...older, traffic_daily: daily }).apps).toBe(2);
});

test("the default card carries the compact figures and does not define itself by bluesky", () => {
  const html = overview(overviewFigures(kv));
  expect(html).toContain("11.9M");
  expect(html).toContain("actors indexed");
  expect(html).toContain("69.1%");
  expect(html).toContain("with avatars");
  expect(html).toContain("219");
  expect(html).toContain("have sent searches");
  expect(html).toContain("100% with handles");
  expect(html).toContain("answered, all time");
  expect(html).not.toContain("hidden");
  expect(html.toLowerCase()).not.toContain("bluesky");
});

test("firstTiles: single letters and Bluesky CDN avatars only", () => {
  const avatar = "https://cdn.bsky.app/img/avatar_thumbnail/plain/did:plc:x/abc@jpeg";
  expect(firstTiles(JSON.stringify([["a", avatar], ["b", ""]]))).toEqual([{ letter: "a", avatar }, { letter: "b", avatar: "" }]);
  for (const bad of [
    [["a", "https://example.com/a.jpg"]], [["a", "http://cdn.bsky.app/x"]], [["ab", avatar]], [["A", avatar]],
    [], Array.from({ length: 27 }, () => ["a", ""]), [["a"]], { a: avatar },
  ]) expect(firstTiles(JSON.stringify(bad))).toBeNull();
  expect(firstTiles("not json")).toBeNull();
  expect(firstTiles(null)).toBeNull();
});

test("firstCard: every letter in order, a blank tile without an avatar, and the title", () => {
  const tiles = [..."abcdefghijklmnopqrstuvwxyz"].map((letter) => ({ letter, avatar: "" }));
  const images = tiles.map((_, i) => (i === 3 ? "" : "data:image/jpeg;base64,AAAA"));
  const card = firstCard(tiles, images);
  const letters = [...card.matchAll(/color:#fff;font-size:22px">([a-z])</g)].map((m) => m[1]).join("");
  expect(letters).toBe("abcdefghijklmnopqrstuvwxyz");
  expect(card.match(/<img /g)).toHaveLength(25);
  expect(card).toContain("who comes up first, a to z");
  expect(card).toContain("typeahead.waow.tech");
});

test("firstCard: a query's card has no letter badges and its own title", () => {
  const tiles = Array.from({ length: 12 }, () => ({ letter: "", avatar: "" }));
  const card = firstCard(tiles, tiles.map(() => "data:image/jpeg;base64,AAAA"), "the nathaniels");
  expect(card).not.toContain("font-size:22px");
  expect(card).toContain(">the nathaniels<");
  expect(card).not.toContain("who comes up first, a to z");
  expect(firstTiles(JSON.stringify([["", "https://cdn.bsky.app/img/x@jpeg"]]))).toEqual([{ letter: "", avatar: "https://cdn.bsky.app/img/x@jpeg" }]);
});

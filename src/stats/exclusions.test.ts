import { expect, test } from "bun:test";
import { EXCLUSIONS_KEY, EXCLUSIONS_REFRESH_MS, exclusionsDue, parseExclusions, type SearchExclusions } from "./exclusions";

const STORED = { version: 2, as_of: "2026-11-01T09:00:00.000Z", policy_hidden: 3, stale_hidden_flags: 0, handle_invalid: 2, quarantined: 2 } satisfies SearchExclusions;
const kv = (value: string | null) => ({
  get: async (key: string) => (key === EXCLUSIONS_KEY ? value : null),
  put: async () => {},
});

test("a count is due when none is stored, or the stored one is a day old", async () => {
  const first = Date.parse(STORED.as_of);
  expect(await exclusionsDue(kv(null), new Date(first))).toBe(true);
  expect(await exclusionsDue(kv("not json"), new Date(first))).toBe(true);
  expect(await exclusionsDue(kv(JSON.stringify(STORED)), new Date(first + EXCLUSIONS_REFRESH_MS - 1))).toBe(false);
  expect(await exclusionsDue(kv(JSON.stringify(STORED)), new Date(first + EXCLUSIONS_REFRESH_MS))).toBe(true);
});

test("an unreadable or older stored shape is treated as missing", () => {
  expect(parseExclusions(null)).toBeNull();
  expect(parseExclusions("not json")).toBeNull();
  expect(parseExclusions(JSON.stringify({ count: 3110, as_of: "2026-10-28" }))).toBeNull();
  expect(parseExclusions(JSON.stringify(STORED))).toEqual(STORED);
});

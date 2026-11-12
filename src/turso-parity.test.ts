import { expect, test } from "bun:test";
import type { ProfileView } from "./handlers/profiles";
import { corpusParity, profileParity, sampleStarts } from "./turso-parity";

const profile = (n: number, changes: Partial<ProfileView> = {}): ProfileView =>
  ({ did: `did:plc:${n}`, handle: `u${n}.test`, labels: [], followersCount: n, indexedAt: "2026-11-01T00:00:00.000Z", ...changes });

test("a sample passes while missing actors and settled-field differences stay within the rate", () => {
  const turso = Array.from({ length: 100 }, (_, n) => profile(n));
  const slot = turso.map((p, n) => (n === 0 ? { ...p, displayName: "renamed" } : { ...p, followersCount: 1_000_000, indexedAt: "2026-11-12T00:00:00.000Z" })).slice(0, 99);
  const report = profileParity(turso, slot, 0.02);
  expect(report).toMatchObject({
    sampled: 100, turso: 100, slot: 99, only_turso: 1, only_slot: 0, settled_differ: 1,
    fields: { displayName: 1, followersCount: 98, indexedAt: 98 }, failing_rate: 0.02, passed: true,
  });
  expect(profileParity(turso, slot, 0.019).passed).toBe(false);
});

test("an empty sample does not pass", () => {
  expect(profileParity([], [], 1).passed).toBe(false);
});

test("sample starts are within the table, and the same seed draws the same sample", () => {
  const starts = sampleStarts(13_000_000, 200, 42);
  expect(starts).toHaveLength(200);
  expect(starts.every((rowid) => Number.isInteger(rowid) && rowid >= 1 && rowid <= 13_000_000)).toBe(true);
  expect(new Set(starts).size).toBeGreaterThan(190);
  expect(sampleStarts(13_000_000, 200, 42)).toEqual(starts);
  expect(sampleStarts(13_000_000, 200, 43)).not.toEqual(starts);
});

test("corpus counts pass within the rate, field by field, and not without both sides", () => {
  const turso = { hour: 1, total: 1000, with_handles: 900, with_avatars: 500, hidden: 10 };
  const near = { hour: 1, total: 1010, with_handles: 891, with_avatars: 505, hidden: 80 };
  const report = corpusParity(turso, near, 0.01);
  expect(report.passed).toBe(true);
  expect(report.differences.map((d) => [d.field, d.rate])).toEqual([["total", 0.01], ["with_handles", 0.01], ["with_avatars", 0.01]]);
  expect(corpusParity(turso, { ...near, with_avatars: 400 }, 0.01).passed).toBe(false);
  expect(corpusParity(turso, null, 1).passed).toBe(false);
  expect(corpusParity(null, near, 1).passed).toBe(false);
});

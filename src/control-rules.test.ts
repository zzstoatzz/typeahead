import { expect, test } from "bun:test";
import { z } from "zod";
import { validBlockedDomain, validOverrideDid } from "./control-rules";
import raw from "./control-rule-vectors.json";

/** One value: literal, or a long one written as its parts. */
const value = z.union([
  z.string(),
  z.object({ prefix: z.string(), fill: z.string(), count: z.number().int(), suffix: z.string() })
    .transform((parts) => parts.prefix + parts.fill.repeat(parts.count) + parts.suffix),
]);
const judged = z.object({ accepted: z.array(value), refused: z.array(value) });
const vectors = z.object({ dids: judged, domains: judged }).parse(raw);

// The same file is read by services/src/store_process/control_test.zig: a
// value accepted here and refused by a slot would be written, served, and
// freeze the policy on every slot.

const shown = (text: string) => JSON.stringify(text.slice(0, 48));

test("override subjects are judged as the vectors say", () => {
  for (const vector of vectors.dids.accepted) expect([shown(vector), validOverrideDid(vector)]).toEqual([shown(vector), true]);
  for (const vector of vectors.dids.refused) expect([shown(vector), validOverrideDid(vector)]).toEqual([shown(vector), false]);
  expect(vectors.dids.accepted.length).toBeGreaterThan(5);
  expect(vectors.dids.refused.length).toBeGreaterThan(10);
});

test("blocked domains are judged as the vectors say", () => {
  for (const vector of vectors.domains.accepted) expect([shown(vector), validBlockedDomain(vector)]).toEqual([shown(vector), true]);
  for (const vector of vectors.domains.refused) expect([shown(vector), validBlockedDomain(vector)]).toEqual([shown(vector), false]);
  expect(vectors.domains.accepted.length).toBeGreaterThan(5);
  expect(vectors.domains.refused.length).toBeGreaterThan(10);
});

test("the bounds are counted in bytes, at the store's limits", () => {
  expect(validOverrideDid("did:plc:" + "a".repeat(2040))).toBe(true);
  expect(validOverrideDid("did:plc:" + "a".repeat(2041))).toBe(false);
  expect(validBlockedDomain("x".repeat(249) + ".com")).toBe(true);
  expect(validBlockedDomain("x".repeat(250) + ".com")).toBe(false);
});

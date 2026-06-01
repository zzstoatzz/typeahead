import { expect, test } from "bun:test";
import { profilesCacheKey } from "./profiles";

test("cache key is independent of actor order", () => {
  expect(profilesCacheKey(["bob.test", "alice.test"])).toBe(profilesCacheKey(["alice.test", "bob.test"]));
});

test("cache key lowercases handles but preserves DID case", () => {
  expect(profilesCacheKey(["Alice.Test"])).toBe(profilesCacheKey(["alice.test"]));
  // DIDs are case-sensitive identifiers — must not collapse distinct DIDs
  expect(profilesCacheKey(["did:plc:ABC"])).not.toBe(profilesCacheKey(["did:plc:abc"]));
});

test("cache key dedupes repeated actors", () => {
  expect(profilesCacheKey(["alice.test", "alice.test"])).toBe(profilesCacheKey(["alice.test"]));
});

test("mixed dids and handles produce a stable key regardless of order", () => {
  const a = profilesCacheKey(["did:plc:abc", "Carol.test", "did:plc:xyz"]);
  const b = profilesCacheKey(["Carol.test", "did:plc:xyz", "did:plc:abc"]);
  expect(a).toBe(b);
});

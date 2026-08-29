import { expect, test } from "bun:test";
import { profilesCacheKey, profileView } from "./profiles";
import type { ProfileRow } from "../types";

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

const row = {
  did: "did:plc:xbtmt2zjwlrfegqvch7fboei",
  handle: "zzstoatzz.io",
  display_name: "nate",
  avatar_url: "bafkreiavatar",
  labels: "[]",
  created_at: "2024-10-17T22:23:42.606Z",
  associated: "{}",
  pds: "https://pds.zzstoatzz.io",
  description: "zig, prefect, atproto",
  banner_url: "bafkreibanner",
  followers_count: 2000,
  follows_count: 500,
  posts_count: 900,
  profile_checked_at: 1_756_000_000,
  updated_at: 1_756_400_000,
} satisfies ProfileRow;

test("profileView renders the profile-card fields", () => {
  const v = profileView(row);
  expect(v.description).toBe("zig, prefect, atproto");
  expect(v.banner).toBe("https://cdn.bsky.app/img/banner/plain/did:plc:xbtmt2zjwlrfegqvch7fboei/bafkreibanner");
  expect(v.followersCount).toBe(2000);
  expect(v.followsCount).toBe(500);
  expect(v.postsCount).toBe(900);
  expect(v.indexedAt).toBe(new Date(1_756_400_000 * 1000).toISOString());
});

test("profileView omits counts until the enrich path has seen the actor", () => {
  const v = profileView({ ...row, profile_checked_at: 0, followers_count: 0, follows_count: 0, posts_count: 0 });
  expect("followersCount" in v).toBe(false);
  expect("followsCount" in v).toBe(false);
  expect("postsCount" in v).toBe(false);
});

test("profileView omits empty description and banner", () => {
  const v = profileView({ ...row, description: "", banner_url: "" });
  expect("description" in v).toBe(false);
  expect("banner" in v).toBe(false);
});

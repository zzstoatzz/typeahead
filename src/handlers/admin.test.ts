import { expect, test } from "bun:test";
import { extractProfileFields } from "../utils";
import { BSKY_MOD_DID } from "../moderation";

// ─── extractProfileFields: how an appview profile is read ─────

test("extractProfileFields: populated profile writes counts + qualityScore", () => {
  // Paul Frazee shape from bsky's getProfiles — 308k followers should give a
  // meaningful quality_score, the WHOLE point of running enrichment.
  const profile = {
    handle: "pfrazee.com",
    displayName: "Paul Frazee",
    avatar: "https://cdn.bsky.app/img/avatar/plain/did:plc:abc/bafkrei123",
    labels: [],
    createdAt: "2023-05-01T00:00:00.000Z",
    followersCount: 308569,
    followsCount: 629,
    postsCount: 37792,
    associated: {},
  };
  const f = extractProfileFields(profile, null, false);
  expect(f.handle).toBe("pfrazee.com");
  expect(f.displayName).toBe("Paul Frazee");
  expect(f.followersCount).toBe(308569);
  expect(f.postsCount).toBe(37792);
  expect(f.hidden).toBe(0);
  // log10(308569)≈5.49 * 100 = 549 + posts contribution + ratio + age — well > 500
  expect(f.qualityScore).toBeGreaterThan(500);
});

test("extractProfileFields: description + banner CID come through", () => {
  const f = extractProfileFields({
    did: "did:plc:x", handle: "x.test", description: "hello",
    banner: "https://cdn.bsky.app/img/banner/plain/did:plc:x/bafkreibanner@jpeg",
    followersCount: 1, followsCount: 1, postsCount: 1,
  });
  expect(f.description).toBe("hello");
  expect(f.bannerCid).toBe("bafkreibanner");
});

test("extractProfileFields: countless profile → qualityScore 0 (no age-bonus clobber)", () => {
  // A getProfiles response that omits aggregate counts (transient appview gap)
  // must NOT score the account on age alone. The age-only 30/60 is non-zero, so
  // it would slip past every NULLIF(?,0) write guard and overwrite a real
  // account's score while its true follower count stays put — exactly how
  // zzstoatzz.io froze at 30 and pfrazee.com at 60. Emit 0 so the guards hold.
  const profile = {
    handle: "zzstoatzz.io",
    displayName: "nate",
    labels: [],
    createdAt: "2023-01-01T00:00:00.000Z", // > 3yr old → would be +60 if scored
    // followersCount / followsCount / postsCount intentionally ABSENT
    associated: {},
  };
  const f = extractProfileFields(profile, null, false);
  expect(f.followersCount).toBe(0);
  expect(f.postsCount).toBe(0);
  expect(f.qualityScore).toBe(0);
});

test("extractProfileFields: genuine zero-follower profile still scores (counts present)", () => {
  // A real account that simply has 0 followers DOES send followersCount: 0.
  // That is an aggregate, not an absence — it should still be scored (age bonus
  // applies), unlike the countless case above.
  const profile = {
    handle: "newbie.bsky.social",
    labels: [],
    createdAt: "2023-01-01T00:00:00.000Z", // > 3yr → +60
    followersCount: 0,
    followsCount: 0,
    postsCount: 0,
    associated: {},
  };
  const f = extractProfileFields(profile, null, false);
  expect(f.qualityScore).toBe(60);
});

test("extractProfileFields: label-hidden profile → hidden=1", () => {
  const profile = {
    handle: "x.bsky.social",
    labels: [{ src: BSKY_MOD_DID, val: "!hide" }],
    followersCount: 100,
  };
  const f = extractProfileFields(profile, null, false);
  expect(f.hidden).toBe(1);
});

test("extractProfileFields: domain-blocked profile (no override) → hidden=1", () => {
  const profile = { handle: "x.dadavidtseng.com", labels: [], followersCount: 0 };
  const f = extractProfileFields(profile, null, true);
  expect(f.hidden).toBe(1);
});

test("extractProfileFields: per-DID show override beats domain-block + labels", () => {
  // The false-positive escape hatch: a rescued account on a blocked domain
  // (or a label-hidden account) stays visible if explicitly show'd.
  const labeled = {
    handle: "x.dadavidtseng.com",
    labels: [{ src: BSKY_MOD_DID, val: "!hide" }],
    followersCount: 0,
  };
  const f = extractProfileFields(labeled, "show", true);
  expect(f.hidden).toBe(0);
});

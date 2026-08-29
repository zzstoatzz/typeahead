import { expect, test } from "bun:test";
import { handleEnrichProfiles } from "./admin";
import { extractProfileFields } from "../utils";
import { BSKY_MOD_DID } from "../moderation";

// Minimal stub: getOverrides + getBlockedDomains call .all<…>(), and we want
// the validation paths to return BEFORE any DB use, so the stub is no-op.
const stubDb: any = {
  prepare: () => ({
    bind: (..._args: any[]) => ({
      all: async () => ({ results: [] }),
      run: async () => ({ meta: { changes: 0 } }),
      first: async () => null,
    }),
    all: async () => ({ results: [] }),
    run: async () => ({ meta: { changes: 0 } }),
    first: async () => null,
  }),
  batch: async () => [],
};
const stubEnv: any = { ADMIN_SECRET: "test-secret" };

function postReq(body: any, auth = "Bearer test-secret") {
  return new Request("https://x/admin/enrich-profiles", {
    method: "POST",
    headers: { Authorization: auth, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ─── extractProfileFields: the canonical writer used by the endpoint ─────

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

// ─── handleEnrichProfiles: body validation + auth ────────────────────────

test("handleEnrichProfiles: missing/wrong Bearer → 401", async () => {
  const res = await handleEnrichProfiles(postReq({ dids: ["did:plc:abc"] }, "Bearer wrong"), stubDb, stubEnv);
  expect(res.status).toBe(401);
});

test("handleEnrichProfiles: body.dids not an array → 400", async () => {
  const res = await handleEnrichProfiles(postReq({ dids: "oops" }), stubDb, stubEnv);
  expect(res.status).toBe(400);
});

test("handleEnrichProfiles: > 100 DIDs → 400 (hard server-side cap)", async () => {
  const tooMany = Array.from({ length: 101 }, (_, i) =>
    `did:plc:${i.toString().padStart(20, "0")}aaaaaaaa`);
  const res = await handleEnrichProfiles(postReq({ dids: tooMany }), stubDb, stubEnv);
  expect(res.status).toBe(400);
});

test("handleEnrichProfiles: invalid DID shape → 400", async () => {
  const res = await handleEnrichProfiles(
    postReq({ dids: ["did:plc:valid", "not-a-did", "did:web:also-valid"] }),
    stubDb, stubEnv,
  );
  expect(res.status).toBe(400);
});

test("handleEnrichProfiles: empty array → 200 with zeros", async () => {
  const res = await handleEnrichProfiles(postReq({ dids: [] }), stubDb, stubEnv);
  expect(res.status).toBe(200);
  const body: any = await res.json();
  expect(body.ok).toBe(true);
  expect(body.requested).toBe(0);
  expect(body.profiles_found).toBe(0);
  expect(body.written).toBe(0);
  expect(body.hidden).toBe(0);
  expect(body.errors).toEqual([]);
});

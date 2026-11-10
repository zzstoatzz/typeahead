import { z } from "zod";
import { computeAuthorityScoreBreakdown, extractProfileFields, type ProfileFields } from "./utils";

/** Shared test vectors for the profile mapping and the quality score: the
 *  Zig port in `services/src/store_process/profile.zig` must reproduce every
 *  `expected` here. Regenerate with `bun scripts/profile-vectors.ts`. */

export const VECTOR_NOW_MS = 1_794_300_000_000;

const DAY_MS = 86_400_000;
const MOD = "did:plc:ar7c4by46qjdydhdevvrndac";
const DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const CID = "bafkreiegehiwbazkkpp5nucenajtdjwinxrrnz7ukawpqnudbr47f44zhq";

type Override = "show" | "hide" | null;

export type ProfileCase = {
  name: string;
  profile: unknown;
  override: Override;
  domain_blocked: boolean;
  expected: ProfileFields & { qualityScoreF32Bits: number };
};

/** followers, follows, posts, createdAt in ms or null, score, f32 bits */
export type ScoreRow = [number, number, number, number | null, number, number];

export type ProfileVectors = { now_ms: number; cases: ProfileCase[]; scores: ScoreRow[] };

function f32Bits(value: number): number {
  const view = new DataView(new ArrayBuffer(4));
  view.setFloat32(0, value);
  return view.getUint32(0);
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

type WireLabel = { src: string; uri: string; cid: string; val: string; cts: string; neg?: boolean; exp?: string; ver?: number };

function label(val: string, extra: Partial<WireLabel> = {}): WireLabel {
  return { src: MOD, uri: DID, cid: CID, val, cts: "2026-01-02T03:04:05.000Z", ...extra };
}

const counts = { followersCount: 1234, followsCount: 321, postsCount: 4567 };
const base = { did: DID, handle: "alice.example.com", displayName: "Alice", createdAt: iso(VECTOR_NOW_MS - 400 * DAY_MS), ...counts };

type Input = { name: string; profile: unknown; override?: Override; domain_blocked?: boolean };

const synthetic: Input[] = [
  { name: "no counts at all, as a PDS record gives", profile: { did: DID, handle: "", displayName: "From A Record", avatar: CID, labels: [], createdAt: "2024-03-01T00:00:00.000Z", associated: {} } },
  { name: "zero counts on an old account score the age bonus", profile: { ...base, followersCount: 0, followsCount: 0, postsCount: 0, createdAt: iso(VECTOR_NOW_MS - 1200 * DAY_MS) } },
  { name: "zero counts and no createdAt score zero", profile: { did: DID, handle: "zero.example.com", followersCount: 0, followsCount: 0, postsCount: 0 } },
  { name: "null counts are not aggregates", profile: { ...base, followersCount: null, followsCount: null, postsCount: null } },
  { name: "one count present is enough", profile: { did: DID, handle: "one.example.com", postsCount: 9 } },
  { name: "under a day old", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - DAY_MS + 1) } },
  { name: "exactly a day old", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - DAY_MS) } },
  { name: "under three days old", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - 3 * DAY_MS + 1) } },
  { name: "a fresh account with one follower floors at zero", profile: { ...base, followersCount: 1, followsCount: 0, postsCount: 0, createdAt: iso(VECTOR_NOW_MS - 1000) } },
  { name: "created in the future", profile: { ...base, createdAt: iso(VECTOR_NOW_MS + DAY_MS) } },
  { name: "exactly 365 days old", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - 365 * DAY_MS) } },
  { name: "a millisecond over 365 days", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - 365 * DAY_MS - 1) } },
  { name: "a millisecond over three years", profile: { ...base, createdAt: iso(VECTOR_NOW_MS - 1095 * DAY_MS - 1) } },
  { name: "createdAt with an offset", profile: { ...base, createdAt: "2023-05-06T07:08:09.123+02:00" } },
  { name: "createdAt at year one", profile: { ...base, createdAt: "0001-01-01T00:00:00.000Z" } },
  { name: "createdAt that is not a date", profile: { ...base, createdAt: "not a date" } },
  { name: "ratio of exactly ten earns nothing", profile: { ...base, followersCount: 1000, followsCount: 100 } },
  { name: "ratio over ten", profile: { ...base, followersCount: 1001, followsCount: 100 } },
  { name: "ratio of exactly a hundred", profile: { ...base, followersCount: 10000, followsCount: 100 } },
  { name: "ratio over a hundred", profile: { ...base, followersCount: 10001, followsCount: 100 } },
  { name: "a hundred followers is not over a hundred", profile: { ...base, followersCount: 100, followsCount: 1 } },
  { name: "follows of zero earn no ratio bonus", profile: { ...base, followersCount: 500000, followsCount: 0 } },
  { name: "tens of millions of followers", profile: { ...base, followersCount: 35608278, followsCount: 12, postsCount: 2931 } },
  { name: "a hiding label", profile: { ...base, labels: [label("!hide")] } },
  { name: "a negated hiding label", profile: { ...base, labels: [label("!takedown", { neg: true })] } },
  { name: "an expired hiding label", profile: { ...base, labels: [label("spam", { exp: iso(VECTOR_NOW_MS - 1000) })] } },
  { name: "a hiding label that expires later", profile: { ...base, labels: [label("!suspend", { exp: iso(VECTOR_NOW_MS + DAY_MS) })] } },
  { name: "a hiding value from another labeler", profile: { ...base, labels: [label("spam", { src: "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb" })] } },
  { name: "labels on the account and the profile record", profile: { ...base, labels: [
    label("!no-unauthenticated", { uri: `at://${DID}/app.bsky.actor.profile/self`, src: DID, ver: 1 }),
    label("porn", { ver: 1 }),
    label("spam", { neg: true, ver: 1 }),
  ] } },
  { name: "a show override beats a takedown", profile: { ...base, labels: [label("!takedown")] }, override: "show" },
  { name: "a hide override on a clean profile", profile: base, override: "hide" },
  { name: "a blocked domain", profile: base, domain_blocked: true },
  { name: "a blocked domain with a show override", profile: base, override: "show", domain_blocked: true },
  { name: "no Bluesky profile, only an identity", profile: { did: DID, handle: "bare.example.com" } },
  { name: "nothing but a DID", profile: { did: DID } },
  { name: "a very long display name and description", profile: { ...base, displayName: "N".repeat(640), description: "long ".repeat(500) } },
  { name: "names outside ASCII", profile: { ...base, displayName: "Łink 日本語 🧑‍🚀 مرحبا é", description: "tab\there \"quoted\" back\\slash\nnewline" } },
  { name: "a CDN avatar and banner", profile: { ...base, avatar: `https://cdn.bsky.app/img/avatar/plain/${DID}/${CID}@jpeg`, banner: `https://cdn.bsky.app/img/banner/plain/${DID}/${CID}@jpeg` } },
  { name: "a CDN avatar without a suffix", profile: { ...base, avatar: `https://cdn.bsky.app/img/avatar/plain/${DID}/${CID}` } },
  { name: "a getBlob avatar", profile: { ...base, avatar: `https://pds.example.com/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(DID)}&cid=${CID}` } },
  { name: "a getBlob avatar with an encoded cid first", profile: { ...base, avatar: `https://pds.example.com/xrpc/com.atproto.sync.getBlob?cid=baf%6Breiabc&did=x` } },
  { name: "a bare CID avatar", profile: { ...base, avatar: CID } },
  { name: "an avatar URL ending in a slash", profile: { ...base, avatar: "https://cdn.example.com/a/b/" } },
  { name: "an avatar segment with an upper-case suffix", profile: { ...base, avatar: "https://cdn.example.com/a/cid@JPEG" } },
  { name: "an avatar segment that is only a suffix", profile: { ...base, avatar: "https://cdn.example.com/a/@jpeg" } },
  { name: "associated with nothing truthy", profile: { ...base, associated: { lists: 0, feedgens: 0, starterPacks: 0, labeler: false, chat: null } } },
  { name: "associated keeps six members and drops the rest", profile: { ...base, associated: {
    germ: { showButtonTo: "none" }, activitySubscription: { allowSubscriptions: "followers" }, chat: { allowIncoming: "all", allowGroupInvites: "all" },
    labeler: true, starterPacks: 4, feedgens: 3, lists: 7,
  } } },
  { name: "associated values that need escaping", profile: { ...base, associated: { chat: { allowIncoming: "a \"b\" \\ \n \u0001 é" } } } },
];

function scoreRows(now: number): ScoreRow[] {
  const rows: ScoreRow[] = [];
  let state = 20261110;
  const next = (bound: number): number => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return Math.floor((state / 4294967296) * bound);
  };
  const magnitude = (): number => next(10 ** (1 + next(8)));
  for (let i = 0; i < 2000; i++) {
    const followers = magnitude();
    const follows = next(4) === 0 ? 0 : magnitude();
    const posts = magnitude();
    const created = next(8) === 0 ? null : now - next(6 * 365) * DAY_MS - next(DAY_MS);
    const counted: Parameters<typeof computeAuthorityScoreBreakdown>[0] = { followersCount: followers, followsCount: follows, postsCount: posts };
    if (created !== null) counted.createdAt = iso(created);
    const score = computeAuthorityScoreBreakdown(counted, now).score;
    rows.push([followers, follows, posts, created, score, f32Bits(score)]);
  }
  return rows;
}

/** `extractProfileFields` reads the clock for the score and for label
 *  expiry, so the clock is pinned while the vectors are built. */
export function buildVectors(appviewProfiles: unknown[]): ProfileVectors {
  const realNow = Date.now;
  Date.now = () => VECTOR_NOW_MS;
  try {
    const inputs: Input[] = [
      ...appviewProfiles.map((profile, index) => ({ name: `appview profile ${index + 1}`, profile })),
      ...synthetic,
    ];
    const cases = inputs.map(({ name, profile, override = null, domain_blocked = false }) => {
      const fields = extractProfileFields(profile, override, domain_blocked);
      return { name, profile, override, domain_blocked, expected: { ...fields, qualityScoreF32Bits: f32Bits(fields.qualityScore) } };
    });
    return { now_ms: VECTOR_NOW_MS, cases, scores: scoreRows(VECTOR_NOW_MS) };
  } finally {
    Date.now = realNow;
  }
}

const capturedSchema = z.object({ profiles: z.array(z.unknown()) });

/** the profiles of a captured `getProfiles` response body */
export function capturedProfiles(body: string): unknown[] {
  return capturedSchema.parse(JSON.parse(body)).profiles;
}

export function renderVectors(vectors: ProfileVectors): string {
  const { scores, ...rest } = vectors;
  const body = JSON.stringify(rest, null, 1);
  const rows = scores.map((row) => `  ${JSON.stringify(row)}`).join(",\n");
  return `${body.slice(0, -2)},\n "scores": [\n${rows}\n ]\n}\n`;
}

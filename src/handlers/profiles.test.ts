import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import { handleGetProfile, handleGetProfiles, profilesCacheKey, slotProfileView, type ProfileView } from "./profiles";

// SAFETY: bun has no `caches` global; the handler uses only default.match and
// default.put, which this always-missing stub provides.
const cacheHost = globalThis as { caches?: { default: { match(): Promise<undefined>; put(): Promise<void> } } };
cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };

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

const ALICE = "did:plc:alicealicealicealicealice";
const BOB = "did:plc:bobbobbobbobbobbobbobbob";
const HIDDEN = "did:plc:hiddenhiddenhiddenhidden";

function slotActor(did: string, handle: string, changes: Partial<SlotActor> = {}): SlotActor {
  return {
    did, handle, display_name: "", avatar_url: "", labels: [], created_at: "", associated: {}, pds: "", description: "",
    banner_url: "", followers_count: 0, follows_count: 0, posts_count: 0, quality_score: 0, updated_at: 0,
    profile_checked: false, hidden: false, hidden_stored: false, ...changes,
  };
}

const SLOT_ALICE = slotActor(ALICE, "alice.test", {
  display_name: "Alice", avatar_url: "bafkreialice",
  labels: [{ cts: "2026-01-01T00:00:00Z", src: "did:plc:labeler", uri: "did:plc:x", val: "spam" }],
  created_at: "2024-10-17T22:23:42.606Z", associated: { chat: { allowIncoming: "all" }, labeler: true },
  pds: "https://pds.alice.test", description: "line one\nline two", banner_url: "bafkreibanner",
  followers_count: 2000, follows_count: 500, posts_count: 900, quality_score: 12.5, updated_at: 1756400000, profile_checked: true,
});
const SLOT_BOB = slotActor(BOB, "bob.test", { updated_at: 1756400001 });
const SLOT_HIDDEN = slotActor(HIDDEN, "hidden.test", { updated_at: 1756400002, hidden: true, hidden_stored: true });

type Slot = { asked: string[]; answer: () => Response };

function slot(actors: SlotActor[]): Slot {
  return { asked: [], answer: () => Response.json({ actors }) };
}

function env(backend: Slot): Env {
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      backend.asked.push(String(input));
      return backend.answer();
    },
  };
  // SAFETY: the handlers reach the slot only through SEARCH_BACKEND_URL and
  // SEARCH_TRANSPORT.fetch, and read nothing else.
  return { SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport } as Env;
}

function background() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (task: Promise<unknown>) => { tasks.push(task); }, passThroughOnException() {} };
  // SAFETY: the handlers use the context only through waitUntil.
  return { ctx: c as ExecutionContext, tasks };
}

const getProfiles = (actors: string[]) =>
  new Request(`https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfiles?${actors.map((a) => `actors=${encodeURIComponent(a)}`).join("&")}`);

let logged: string[] = [];
let log: ReturnType<typeof spyOn>;
beforeEach(() => {
  logged = [];
  log = spyOn(console, "log").mockImplementation((line: string) => { logged.push(line); });
});
afterEach(() => { log.mockRestore(); });
const body = async (res: Response) => JSON.parse(await res.text());
const events = (name: string) => logged.map((line) => JSON.parse(line)).filter((entry) => entry.event === name);

/** the wire form, keys in the order the response serializes them */
const EXPECTED_ALICE = {
  did: ALICE,
  handle: "alice.test",
  displayName: "Alice",
  description: "line one\nline two",
  avatar: `https://cdn.bsky.app/img/avatar/plain/${ALICE}/bafkreialice`,
  avatarFallback: `https://pds.alice.test/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(ALICE)}&cid=bafkreialice`,
  banner: `https://cdn.bsky.app/img/banner/plain/${ALICE}/bafkreibanner`,
  followersCount: 2000,
  followsCount: 500,
  postsCount: 900,
  associated: { chat: { allowIncoming: "all" }, labeler: true },
  labels: [{ cts: "2026-01-01T00:00:00Z", src: "did:plc:labeler", uri: "did:plc:x", val: "spam" }],
  createdAt: "2024-10-17T22:23:42.606Z",
  indexedAt: "2025-08-28T16:53:20.000Z",
};
const EXPECTED_BOB = { did: BOB, handle: "bob.test", labels: [], indexedAt: "2025-08-28T16:53:21.000Z" };

test("profiles are answered from the slot, with the wire fields in their order", async () => {
  const backend = slot([SLOT_ALICE, SLOT_BOB]);
  const { ctx, tasks } = background();
  const res = await handleGetProfiles(getProfiles([ALICE, "bob.test"]), env(backend), ctx);
  expect(res.status).toBe(200);
  expect(backend.asked).toEqual([`https://transport.test/actors?actor=${encodeURIComponent(ALICE)}&actor=bob.test`]);
  expect(res.headers.get("Cache-Control")).toBe("public, max-age=300, s-maxage=300");
  expect(res.headers.get("Content-Type")).toBe("application/json");
  expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
  expect(await res.text()).toBe(JSON.stringify({ profiles: [EXPECTED_ALICE, EXPECTED_BOB] }));
  expect(tasks).toHaveLength(1);
  await Promise.all(tasks);
});

test("no actors and more than 25 are refused before the slot is asked", async () => {
  const backend = slot([]);
  const none = await handleGetProfiles(getProfiles([]), env(backend), background().ctx);
  expect(none.status).toBe(400);
  expect(await body(none)).toEqual({ error: "InvalidRequest", message: "at least one 'actors' param required" });
  const many = await handleGetProfiles(getProfiles(Array.from({ length: 26 }, (_, i) => `did:plc:${i}`)), env(backend), background().ctx);
  expect(many.status).toBe(400);
  expect(await body(many)).toEqual({ error: "InvalidRequest", message: "max 25 actors per request" });
  expect(backend.asked).toEqual([]);
});

test("hidden actors and excluded handles are left out, and the order asked for is kept", async () => {
  const backend = slot([SLOT_BOB, SLOT_HIDDEN, slotActor("did:plc:unverified", "handle.invalid"), SLOT_ALICE]);
  const res = await handleGetProfiles(getProfiles(["bob.test", HIDDEN, "did:plc:unverified", ALICE]), env(backend), background().ctx);
  const answer: { profiles: ProfileView[] } = await res.json();
  expect(answer.profiles.map((p) => p.did)).toEqual([BOB, ALICE]);
});

test("a view renders the profile-card fields", () => {
  const v = slotProfileView(SLOT_ALICE);
  expect(v.description).toBe("line one\nline two");
  expect(v.banner).toBe(`https://cdn.bsky.app/img/banner/plain/${ALICE}/bafkreibanner`);
  expect(v.followersCount).toBe(2000);
  expect(v.followsCount).toBe(500);
  expect(v.postsCount).toBe(900);
  expect(v.indexedAt).toBe(new Date(1_756_400_000 * 1000).toISOString());
});

test("a view shows counts once the store has read the profile, even when all three are zero", () => {
  const unread = slotProfileView(SLOT_BOB);
  expect("followersCount" in unread).toBe(false);
  expect("followsCount" in unread).toBe(false);
  expect("postsCount" in unread).toBe(false);
  const read = slotProfileView({ ...SLOT_BOB, profile_checked: true });
  expect([read.followersCount, read.followsCount, read.postsCount]).toEqual([0, 0, 0]);
  expect("indexedAt" in slotProfileView({ ...SLOT_BOB, updated_at: 0 })).toBe(false);
});

test("a view keeps known counts for a profile not marked as read", () => {
  const v = slotProfileView({ ...SLOT_ALICE, profile_checked: false });
  expect(v.followersCount).toBe(2000);
  expect(v.followsCount).toBe(500);
  expect(v.postsCount).toBe(900);
});

test("a view omits an empty description, banner and associated", () => {
  const v = slotProfileView({ ...SLOT_ALICE, description: "", banner_url: "", associated: {} });
  expect("description" in v).toBe(false);
  expect("banner" in v).toBe(false);
  expect("associated" in v).toBe(false);
});

test("a slot that does not answer is a 503 that is not cached", async () => {
  for (const answer of [
    () => Response.json({ error: "SearchBackendIdentityMismatch" }, { status: 503 }),
    () => Response.json({ actors: [{ did: ALICE }] }),
    () => new Response("not json"),
    () => { throw new DOMException("timed out", "TimeoutError"); },
  ]) {
    const { ctx, tasks } = background();
    const res = await handleGetProfiles(getProfiles([ALICE]), env({ asked: [], answer }), ctx);
    expect(res.status).toBe(503);
    expect(await body(res)).toEqual({ error: "UpstreamFailure", message: "profile lookup is temporarily unavailable" });
    expect(tasks).toEqual([]);
    expect(events("profile_slot_failed")).toHaveLength(1);
    logged = [];
  }
});

test("getProfile unwraps the slot's one profile and answers 400 for an actor it does not hold", async () => {
  const found = await handleGetProfile(
    new Request("https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile?actor=alice.test"), env(slot([SLOT_ALICE])), background().ctx);
  expect(found.status).toBe(200);
  expect(await found.text()).toBe(JSON.stringify(EXPECTED_ALICE));

  const missing = await handleGetProfile(
    new Request("https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile?actor=nobody.test"), env(slot([])), background().ctx);
  expect(missing.status).toBe(400);
  expect(await body(missing)).toEqual({ error: "InvalidRequest", message: "Profile not found" });

  const unnamed = await handleGetProfile(
    new Request("https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile"), env(slot([])), background().ctx);
  expect(unnamed.status).toBe(400);
  expect(await body(unnamed)).toEqual({ error: "InvalidRequest", message: "'actor' param required" });
});

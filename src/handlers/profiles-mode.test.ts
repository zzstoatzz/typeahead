import { afterAll, afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { tursoPolicyTables } from "../control-d1-fixture";
import type { SqlDb } from "../db";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import { handleGetProfile, handleGetProfiles, profileDifferences, slotProfileView, type ProfileView } from "./profiles";

// getProfiles and getProfile under PROFILE_MODE: Turso, the routed slot, or
// Turso with the slot's answer compared in the background.

// SAFETY: bun has no `caches` global; the handler uses only default.match and
// default.put, which this always-missing stub provides.
const cacheHost = globalThis as { caches?: { default: { match(): Promise<undefined>; put(): Promise<void> } } };
cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };

const { turso, dispose } = await tursoPolicyTables();
afterAll(dispose);

const ALICE = "did:plc:alicealicealicealicealice";
const BOB = "did:plc:bobbobbobbobbobbobbobbob";
const HIDDEN = "did:plc:hiddenhiddenhiddenhidden";

await turso.batch([
  turso.prepare(
    `INSERT INTO actors (did, handle, display_name, avatar_url, labels, created_at, associated, pds, description, banner_url,
       followers_count, follows_count, posts_count, profile_checked_at, updated_at)
     VALUES (?1, 'alice.test', 'Alice', 'bafkreialice', '[{"src":"did:plc:labeler","uri":"did:plc:x","val":"spam","cts":"2026-01-01T00:00:00Z"}]',
       '2024-10-17T22:23:42.606Z', '{"labeler":true,"chat":{"allowIncoming":"all"}}', 'https://pds.alice.test',
       'line one\nline two', 'bafkreibanner', 2000, 500, 900, 1756000000, 1756400000)`).bind(ALICE),
  turso.prepare("INSERT INTO actors (did, handle, updated_at) VALUES (?1, 'bob.test', 1756400001)").bind(BOB),
  turso.prepare("INSERT INTO actors (did, handle, hidden, updated_at) VALUES (?1, 'hidden.test', 1, 1756400002)").bind(HIDDEN),
]);

function slotActor(did: string, handle: string, changes: Partial<SlotActor> = {}): SlotActor {
  return {
    did, handle, display_name: "", avatar_url: "", labels: [], created_at: "", associated: {}, pds: "", description: "",
    banner_url: "", followers_count: 0, follows_count: 0, posts_count: 0, quality_score: 0, updated_at: 0,
    profile_checked: false, hidden: false, hidden_stored: false, ...changes,
  };
}

/** what a slot holding the same three accounts answers */
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

function env(mode: string | undefined, backend: Slot): Env {
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      backend.asked.push(String(input));
      return backend.answer();
    },
  };
  // SAFETY: the handlers read PROFILE_MODE and reach the slot only through
  // SEARCH_BACKEND_URL and SEARCH_TRANSPORT.fetch.
  return { PROFILE_MODE: mode, SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport } as Env;
}

function background() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (task: Promise<unknown>) => { tasks.push(task); }, passThroughOnException() {} };
  // SAFETY: the handlers use the context only through waitUntil.
  return { ctx: c as ExecutionContext, tasks };
}

const noTurso: SqlDb = {
  prepare() { throw new Error("Turso was read"); },
  batch() { throw new Error("Turso was read"); },
};

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
  associated: { labeler: true, chat: { allowIncoming: "all" } },
  labels: [{ src: "did:plc:labeler", uri: "did:plc:x", val: "spam", cts: "2026-01-01T00:00:00Z" }],
  createdAt: "2024-10-17T22:23:42.606Z",
  indexedAt: "2025-08-28T16:53:20.000Z",
};

test("with no mode, or one it does not know, Turso answers and the slot is not asked", async () => {
  for (const mode of [undefined, "", "turso", "SLOT", "d1"]) {
    const backend = slot([]);
    const { ctx, tasks } = background();
    const res = await handleGetProfiles(getProfiles([ALICE, "hidden.test"]), turso, env(mode, backend), ctx);
    expect(res.status).toBe(200);
    expect(await body(res)).toEqual({ profiles: [EXPECTED_ALICE] });
    await Promise.all(tasks);
    expect(backend.asked).toEqual([]);
  }
});

test("slot mode answers from the slot alone, with the fields and key order Turso's answer has", async () => {
  const fromTurso = await handleGetProfiles(getProfiles([ALICE, "bob.test"]), turso, env("turso", slot([])), background().ctx);
  const backend = slot([SLOT_ALICE, SLOT_BOB]);
  const res = await handleGetProfiles(getProfiles([ALICE, "bob.test"]), noTurso, env("slot", backend), background().ctx);
  expect(res.status).toBe(200);
  expect(backend.asked).toEqual([`https://transport.test/actors?actor=${encodeURIComponent(ALICE)}&actor=bob.test`]);
  expect(res.headers.get("Cache-Control")).toBe(fromTurso.headers.get("Cache-Control"));
  expect(res.headers.get("Content-Type")).toBe("application/json");
  expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");

  const slotBody: { profiles: ProfileView[] } = await res.json();
  const tursoBody: { profiles: ProfileView[] } = await fromTurso.json();
  expect(slotBody.profiles.map((p) => Object.keys(p))).toEqual(tursoBody.profiles.map((p) => Object.keys(p)));
  expect(slotBody).toEqual(tursoBody);
  // the one byte-level difference: nested keys arrive sorted from a slot
  expect(JSON.stringify(slotBody.profiles[1])).toBe(JSON.stringify(tursoBody.profiles[1]));
  expect(JSON.stringify(slotBody.profiles[0]!.associated)).toBe('{"chat":{"allowIncoming":"all"},"labeler":true}');
});

test("slot mode leaves out hidden actors and excluded handles, and keeps the order asked for", async () => {
  const backend = slot([SLOT_BOB, SLOT_HIDDEN, slotActor("did:plc:unverified", "handle.invalid"), SLOT_ALICE]);
  const res = await handleGetProfiles(getProfiles(["bob.test", HIDDEN, "did:plc:unverified", ALICE]), noTurso, env("slot", backend), background().ctx);
  const answer: { profiles: ProfileView[] } = await res.json();
  expect(answer.profiles.map((p) => p.did)).toEqual([BOB, ALICE]);
});

test("slot mode shows counts once the store has read the profile, even when all three are zero", async () => {
  const unread = slotProfileView(SLOT_BOB);
  expect("followersCount" in unread).toBe(false);
  const read = slotProfileView({ ...SLOT_BOB, profile_checked: true });
  expect([read.followersCount, read.followsCount, read.postsCount]).toEqual([0, 0, 0]);
  expect("indexedAt" in slotProfileView({ ...SLOT_BOB, updated_at: 0 })).toBe(false);
});

test("slot mode answers 503 without a cached response when the slot does not answer", async () => {
  for (const answer of [
    () => Response.json({ error: "SearchBackendIdentityMismatch" }, { status: 503 }),
    () => Response.json({ actors: [{ did: ALICE }] }),
    () => new Response("not json"),
    () => { throw new DOMException("timed out", "TimeoutError"); },
  ]) {
    const { ctx, tasks } = background();
    const res = await handleGetProfiles(getProfiles([ALICE]), noTurso, env("slot", { asked: [], answer }), ctx);
    expect(res.status).toBe(503);
    expect(await body(res)).toEqual({ error: "UpstreamFailure", message: "profile lookup is temporarily unavailable" });
    expect(tasks).toEqual([]);
    expect(events("profile_slot_failed")).toHaveLength(1);
    logged = [];
  }
});

test("compare mode serves Turso's answer and logs how the slot's differs", async () => {
  const stale = { ...SLOT_ALICE, description: "an older description", followers_count: 1999, updated_at: 1756300000 };
  const backend = slot([stale, slotActor("did:plc:extra", "bob.test")]);
  const { ctx, tasks } = background();
  const res = await handleGetProfiles(getProfiles([ALICE, "bob.test"]), turso, env("compare", backend), ctx);
  const served: { profiles: ProfileView[] } = await res.json();
  expect(served.profiles.map((p) => p.did).sort()).toEqual([ALICE, BOB].sort());
  expect(served.profiles.find((p) => p.did === ALICE)).toEqual(EXPECTED_ALICE);

  await Promise.all(tasks);
  expect(backend.asked).toHaveLength(1);
  expect(events("profile_compare")).toEqual([{
    event: "profile_compare",
    asked: 2,
    turso: 2,
    slot: 2,
    only_turso: 1,
    only_slot: 1,
    settled_differ: 1,
    fields: { description: 1, followersCount: 1, indexedAt: 1 },
    sample: [
      { did: ALICE, fields: ["description", "followersCount", "indexedAt"] },
      { did: BOB, only: "turso" },
      { did: "did:plc:extra", only: "slot" },
    ],
  }]);
});

test("compare mode reports agreement, and a slot that fails changes nothing the caller sees", async () => {
  const agreeing = background();
  await handleGetProfiles(getProfiles([ALICE, BOB]), turso, env("compare", slot([SLOT_ALICE, SLOT_BOB])), agreeing.ctx);
  await Promise.all(agreeing.tasks);
  expect(events("profile_compare")[0]).toMatchObject({ only_turso: 0, only_slot: 0, settled_differ: 0, fields: {}, sample: [] });

  logged = [];
  const failing = background();
  const answer = () => Response.json({ error: "not ready" }, { status: 503 });
  const res = await handleGetProfiles(getProfiles([ALICE]), turso, env("compare", { asked: [], answer }), failing.ctx);
  expect(await body(res)).toEqual({ profiles: [EXPECTED_ALICE] });
  await Promise.all(failing.tasks);
  expect(events("profile_compare_failed")).toEqual([{ event: "profile_compare_failed", status: 503, error: "the slot answered 503" }]);
});

test("getProfile unwraps the slot's one profile and answers 400 for an actor it does not hold", async () => {
  const found = await handleGetProfile(
    new Request("https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile?actor=alice.test"), noTurso, env("slot", slot([SLOT_ALICE])), background().ctx);
  expect(found.status).toBe(200);
  expect(await body(found)).toEqual({ ...EXPECTED_ALICE, associated: { chat: { allowIncoming: "all" }, labeler: true } });

  const missing = await handleGetProfile(
    new Request("https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile?actor=nobody.test"), noTurso, env("slot", slot([])), background().ctx);
  expect(missing.status).toBe(400);
  expect(await body(missing)).toEqual({ error: "InvalidRequest", message: "Profile not found" });
});

test("differences ignore the order of actors and of nested keys, and name each differing field", () => {
  const a: ProfileView = { did: "did:plc:a", handle: "a.test", associated: { labeler: true, chat: { allowIncoming: "all" } }, labels: [] };
  const b: ProfileView = { did: "did:plc:b", handle: "b.test", labels: [] };
  expect(profileDifferences([a, b], [b, { ...a, associated: { chat: { allowIncoming: "all" }, labeler: true } }])).toEqual([]);
  expect(profileDifferences([a], [{ ...a, handle: "A.test", displayName: "A" }])).toEqual([{ did: "did:plc:a", fields: ["handle", "displayName"] }]);
  expect(profileDifferences([a], [])).toEqual([{ did: "did:plc:a", only: "turso" }]);
  expect(profileDifferences([], [b])).toEqual([{ did: "did:plc:b", only: "slot" }]);
});

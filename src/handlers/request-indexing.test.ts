import { afterAll, afterEach, expect, test } from "bun:test";
import { controlStore } from "../control-d1-fixture";
import { blockDomain, clearOverride, indexRequestsAfter, setOverride, unblockDomain } from "../control-store";
import type { Env } from "../types";
import { CONTROL_FAILURE_KEY, handleControlHealth, noteControlFailure } from "../control-health";
import { BSKY_MOD_DID } from "../moderation";
import { handleRequestIndexing } from "./admin";

// /request-indexing verifies the account, logs the request in the control
// database, which store slots pull, and answers with the account's
// moderation state.

const { d1, control, dispose: disposeControl } = await controlStore();
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
afterAll(disposeControl);

const kv = {
  async get(_key: string): Promise<string | null> { return "1"; },
  async put(_key: string, _value: string): Promise<void> {},
};
// SAFETY: the handler reads CONTROL_DB, and KV only through the relay
// announcement's get and put.
const env = { CONTROL_DB: d1, KV: kv } as Env;

function background() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (task: Promise<unknown>) => { tasks.push(task); }, passThroughOnException() {} };
  // SAFETY: the handler uses the context only through waitUntil.
  return { ctx: c as ExecutionContext, tasks };
}

type Account = { did: string; handle: string; verified?: boolean; profile?: boolean; labels?: { src: string; val: string }[] };
const ALICE: Account = { did: "did:plc:alice", handle: "alice.test" };

/** The four upstreams the handler asks, answering for the accounts given. */
function upstreams(accounts: Account[]): void {
  const reply = <T extends object>(value: T | null) => value === null ? new Response("not found", { status: 404 }) : Response.json(value);
  const fake = async (input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.hostname === "slingshot.microcosm.blue") {
      const asked = url.searchParams.get("identifier");
      const found = accounts.find((account) => account.did === asked || account.handle === asked);
      return reply(found ? { did: found.did, handle: found.handle, pds: "https://pds.test" } : null);
    }
    if (url.hostname === "plc.directory") {
      const did = decodeURIComponent(url.pathname.slice(1));
      const found = accounts.find((account) => account.did === did);
      return reply(found ? { id: did, alsoKnownAs: [found.verified === false ? "at://someone-else.test" : `at://${found.handle}`] } : null);
    }
    if (url.hostname === "cloudflare-dns.com") {
      const handle = (url.searchParams.get("name") ?? "").replace(/^_atproto\./, "");
      const found = accounts.find((account) => account.handle === handle);
      return reply({ Status: 0, Answer: found ? [{ type: 16, data: `"did=${found.did}"` }] : [] });
    }
    if (url.hostname === "public.api.bsky.app") {
      const found = accounts.find((account) => account.did === url.searchParams.get("actor"));
      return reply(found && found.profile !== false
        ? { did: found.did, handle: found.handle, displayName: "Alice", followersCount: 10, followsCount: 5, postsCount: 3, labels: found.labels ?? [] }
        : null);
    }
    return reply(null);
  };
  globalThis.fetch = Object.assign(fake, { preconnect: realFetch.preconnect });
}

const post = (query: string) => new Request(`https://typeahead.waow.tech/request-indexing${query}`, { method: "POST" });
const body = async (res: Response) => JSON.parse(await res.text());
const requests = async () => (await indexRequestsAfter(control, 0, 100)).requests;

test("a verified account is answered and its request is logged for the slots", async () => {
  upstreams([ALICE]);
  const { ctx, tasks } = background();
  const res = await handleRequestIndexing(post("?handle=alice.test"), env, ctx);
  expect(res.status).toBe(200);
  expect(await res.text()).toBe('{"handle":"alice.test","did":"did:plc:alice","hidden":false}');
  expect(res.headers.get("Server-Timing")).toMatch(/^slingshot;dur=\d+, control_write;dur=\d+, override;dur=\d+, blocked_domains;dur=\d+, bsky_profile;dur=\d+$/);
  await Promise.all(tasks);
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
});

test("a DID is accepted as well as a handle, and a repeat inside the window logs nothing more", async () => {
  upstreams([ALICE]);
  const res = await handleRequestIndexing(post("?did=did:plc:alice"), env, background().ctx);
  expect(await body(res)).toEqual({ handle: "alice.test", did: "did:plc:alice", hidden: false });
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
});

test("a request without an identifier, for an unknown one or for an unverified handle is refused and logs nothing", async () => {
  upstreams([ALICE, { did: "did:plc:mallory", handle: "mallory.test", verified: false }]);
  const empty = await handleRequestIndexing(post(""), env, background().ctx);
  expect(empty.status).toBe(400);
  expect(await body(empty)).toEqual({ error: "enter a handle or DID to request indexing." });

  const unknown = await handleRequestIndexing(post("?handle=nobody.test"), env, background().ctx);
  expect(unknown.status).toBe(404);
  expect(await body(unknown)).toEqual({ error: 'could not resolve "nobody.test". check that it\'s a valid handle or DID.' });

  const unverified = await handleRequestIndexing(post("?handle=mallory.test"), env, background().ctx);
  expect(unverified.status).toBe(422);
  expect(await body(unverified)).toEqual({ error: "handle ownership could not be verified" });

  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
});

test("the moderation answer comes from the control record", async () => {
  const bob: Account = { did: "did:plc:bob", handle: "bob.spam.example" };
  upstreams([bob]);

  await setOverride(control, bob.did, "hide", "", 1_794_400_000);
  const overridden = await handleRequestIndexing(post("?handle=bob.spam.example"), env, background().ctx);
  expect(await overridden.text()).toBe('{"handle":"bob.spam.example","did":"did:plc:bob","hidden":true,"reason":"hidden by moderation"}');
  await clearOverride(control, bob.did);

  await blockDomain(control, "spam.example", "", 1_794_400_000);
  const blocked = await handleRequestIndexing(post("?handle=bob.spam.example"), env, background().ctx);
  expect(await body(blocked)).toEqual({ handle: "bob.spam.example", did: "did:plc:bob", hidden: true, reason: "hidden by moderation" });
  await unblockDomain(control, "spam.example");

  const clear = await handleRequestIndexing(post("?handle=bob.spam.example"), env, background().ctx);
  expect(await body(clear)).toEqual({ handle: "bob.spam.example", did: "did:plc:bob", hidden: false });
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }, { seq: 2, did: "did:plc:bob" }]);
});

test("an account the appview does not know is still logged, and reported visible", async () => {
  upstreams([{ did: "did:web:carol.test", handle: "carol.test", profile: false }]);
  const fake = globalThis.fetch;
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.hostname === "carol.test" && url.pathname === "/.well-known/did.json") {
      return Response.json({ id: "did:web:carol.test", alsoKnownAs: ["at://carol.test"] });
    }
    return fake(input);
  }, { preconnect: realFetch.preconnect });
  const res = await handleRequestIndexing(post("?handle=carol.test"), env, background().ctx);
  expect(await body(res)).toEqual({ handle: "carol.test", did: "did:web:carol.test", hidden: false });
  expect((await requests()).at(-1)).toEqual({ seq: 3, did: "did:web:carol.test" });
});

test("an account the appview labels as taken down is reported hidden, unless an override shows it", async () => {
  const frank: Account = { did: "did:plc:frank", handle: "frank.test", labels: [{ src: BSKY_MOD_DID, val: "!takedown" }] };
  upstreams([frank]);
  const labelled = await handleRequestIndexing(post("?handle=frank.test"), env, background().ctx);
  expect(await body(labelled)).toEqual({ handle: "frank.test", did: "did:plc:frank", hidden: true, reason: "hidden by moderation" });

  await setOverride(control, frank.did, "show", "", 1_794_400_000);
  const shown = await handleRequestIndexing(post("?handle=frank.test"), env, background().ctx);
  expect(await body(shown)).toEqual({ handle: "frank.test", did: "did:plc:frank", hidden: false });
  await clearOverride(control, frank.did);
});

test("with the control database down or unmigrated the request is answered from the account's labels alone, and the failure is kept", async () => {
  const unmigrated = await controlStore();
  for (const table of ["mod_overrides", "blocked_domains", "control_meta", "index_requests"]) await unmigrated.d1.exec(`DROP TABLE ${table}`);
  const kept = new Map<string, string>();
  const recording = {
    async get(key: string): Promise<string | null> { return key.startsWith("relay_crawl:") ? "1" : kept.get(key) ?? null; },
    async put(key: string, value: string): Promise<void> { kept.set(key, value); },
  };
  upstreams([
    { did: "did:plc:dave", handle: "dave.farm.example", labels: [{ src: BSKY_MOD_DID, val: "!takedown" }] },
    { did: "did:plc:erin", handle: "erin.test" },
  ]);
  const logged = await requests();
  // SAFETY: as `env` above; the second has no control binding at all.
  const down = [{ CONTROL_DB: unmigrated.d1, KV: recording } as Env, { KV: recording } as Env];
  try {
    for (const [i, failing] of down.entries()) {
      kept.clear();
      const { ctx, tasks } = background();
      const res = await handleRequestIndexing(post("?handle=dave.farm.example"), failing, ctx);
      expect(res.status).toBe(200);
      expect(await body(res)).toEqual({ handle: "dave.farm.example", did: "did:plc:dave", hidden: true, reason: "hidden by moderation" });
      expect(res.headers.get("Server-Timing")).toMatch(/^slingshot;dur=\d+, control_write;dur=\d+, blocked_domains;dur=\d+, bsky_profile;dur=\d+$/);
      await Promise.all(tasks);
      const failure = JSON.parse(kept.get(CONTROL_FAILURE_KEY) ?? "null");
      expect(failure.what).toBe("index_request");
      expect(failure.at).toBeGreaterThan(1_700_000_000_000);

      const health = await handleControlHealth(failing);
      expect([i, health.status]).toEqual([i, 503]);
      const reported = await body(health);
      expect(reported.ok).toBe(false);
      expect(reported.last_failure.what).toBe("index_request");
    }
    const visible = await handleRequestIndexing(post("?handle=erin.test"), down[0]!, background().ctx);
    expect(await body(visible)).toEqual({ handle: "erin.test", did: "did:plc:erin", hidden: false });
  } finally {
    await unmigrated.dispose();
  }
  expect(await requests()).toEqual(logged);
});

test("the control health endpoint answers 200 from a migrated database, with the last failure if there was one", async () => {
  const kept = new Map<string, string>();
  const recording = {
    async get(key: string): Promise<string | null> { return kept.get(key) ?? null; },
    async put(key: string, value: string): Promise<void> { kept.set(key, value); },
  };
  // SAFETY: the health probe reads CONTROL_DB and KV.get.
  const healthy = { CONTROL_DB: d1, KV: recording } as Env;
  const res = await handleControlHealth(healthy);
  expect(res.status).toBe(200);
  expect(res.headers.get("Cache-Control")).toBe("no-store");
  expect(await body(res)).toEqual({ ok: true, revision: 7, last_failure: null });

  const { ctx, tasks } = background();
  noteControlFailure(ctx, healthy, "index_request", "D1_ERROR: no such table: index_requests");
  await Promise.all(tasks);
  expect((await body(await handleControlHealth(healthy))).last_failure).toMatchObject({ what: "index_request", error: "D1_ERROR: no such table: index_requests" });
});

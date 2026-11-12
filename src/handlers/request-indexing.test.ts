import { afterAll, afterEach, expect, test } from "bun:test";
import { controlStore, tursoPolicyTables } from "../control-d1-fixture";
import { blockDomain, clearOverride, indexRequestsAfter, setOverride, unblockDomain } from "../control-store";
import type { Env } from "../types";
import { handleRequestIndexing } from "./admin";

// /request-indexing answers as it did when it wrote only Turso. What it adds
// is a row in the control database's request log, which store slots pull.

const { d1, control, dispose: disposeControl } = await controlStore();
const { turso, dispose: disposeTurso } = await tursoPolicyTables();
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
afterAll(async () => {
  disposeTurso();
  await disposeControl();
});

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

type Account = { did: string; handle: string; verified?: boolean; profile?: boolean };
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
        ? { did: found.did, handle: found.handle, displayName: "Alice", followersCount: 10, followsCount: 5, postsCount: 3, labels: [] }
        : null);
    }
    return reply(null);
  };
  globalThis.fetch = Object.assign(fake, { preconnect: realFetch.preconnect });
}

const post = (query: string) => new Request(`https://typeahead.waow.tech/request-indexing${query}`, { method: "POST" });
const body = async (res: Response) => JSON.parse(await res.text());
const requests = async () => (await indexRequestsAfter(control, 0, 100)).requests;
const stored = (did: string) =>
  turso.prepare("SELECT handle, display_name, hidden, pds FROM actors WHERE did = ?1").bind(did).first<{ handle: string; display_name: string; hidden: number; pds: string }>();

test("a verified account is answered as before and its request is logged for the slots", async () => {
  upstreams([ALICE]);
  const { ctx, tasks } = background();
  const res = await handleRequestIndexing(post("?handle=alice.test"), turso, env, ctx);
  expect(res.status).toBe(200);
  expect(await body(res)).toEqual({ handle: "alice.test", did: "did:plc:alice", hidden: false });
  expect(res.headers.get("Server-Timing")).toContain("control_write;dur=");
  await Promise.all(tasks);
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
  expect({ ...await stored("did:plc:alice") }).toEqual({ handle: "alice.test", display_name: "Alice", hidden: 0, pds: "https://pds.test" });
});

test("a DID is accepted as well as a handle, and a repeat inside the window logs nothing more", async () => {
  upstreams([ALICE]);
  const res = await handleRequestIndexing(post("?did=did:plc:alice"), turso, env, background().ctx);
  expect(await body(res)).toEqual({ handle: "alice.test", did: "did:plc:alice", hidden: false });
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
});

test("requests that were refused before are refused the same way and log nothing", async () => {
  upstreams([ALICE, { did: "did:plc:mallory", handle: "mallory.test", verified: false }]);
  const empty = await handleRequestIndexing(post(""), turso, env, background().ctx);
  expect(empty.status).toBe(400);
  expect(await body(empty)).toEqual({ error: "enter a handle or DID to request indexing." });

  const unknown = await handleRequestIndexing(post("?handle=nobody.test"), turso, env, background().ctx);
  expect(unknown.status).toBe(404);
  expect(await body(unknown)).toEqual({ error: 'could not resolve "nobody.test". check that it\'s a valid handle or DID.' });

  const unverified = await handleRequestIndexing(post("?handle=mallory.test"), turso, env, background().ctx);
  expect(unverified.status).toBe(422);
  expect(await body(unverified)).toEqual({ error: "handle ownership could not be verified" });

  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }]);
  expect(await stored("did:plc:mallory")).toBeNull();
});

test("the moderation answer comes from the control record", async () => {
  const bob: Account = { did: "did:plc:bob", handle: "bob.spam.example" };
  upstreams([bob]);

  await setOverride(control, bob.did, "hide", "", 1_794_400_000);
  const overridden = await handleRequestIndexing(post("?handle=bob.spam.example"), turso, env, background().ctx);
  expect(await body(overridden)).toEqual({ handle: "bob.spam.example", did: "did:plc:bob", hidden: true, reason: "hidden by moderation" });
  expect((await stored(bob.did))?.hidden).toBe(1);
  await clearOverride(control, bob.did);

  await blockDomain(control, "spam.example", "", 1_794_400_000);
  const blocked = await handleRequestIndexing(post("?handle=bob.spam.example"), turso, env, background().ctx);
  expect(await body(blocked)).toEqual({ handle: "bob.spam.example", did: "did:plc:bob", hidden: true, reason: "hidden by moderation" });
  await unblockDomain(control, "spam.example");

  const clear = await handleRequestIndexing(post("?handle=bob.spam.example"), turso, env, background().ctx);
  expect(await body(clear)).toEqual({ handle: "bob.spam.example", did: "did:plc:bob", hidden: false });
  expect(await requests()).toEqual([{ seq: 1, did: "did:plc:alice" }, { seq: 2, did: "did:plc:bob" }]);
});

test("an account the appview does not know is still logged and stored bare", async () => {
  upstreams([{ did: "did:web:carol.test", handle: "carol.test", profile: false }]);
  const fake = globalThis.fetch;
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.hostname === "carol.test" && url.pathname === "/.well-known/did.json") {
      return Response.json({ id: "did:web:carol.test", alsoKnownAs: ["at://carol.test"] });
    }
    return fake(input);
  }, { preconnect: realFetch.preconnect });
  const res = await handleRequestIndexing(post("?handle=carol.test"), turso, env, background().ctx);
  expect(await body(res)).toEqual({ handle: "carol.test", did: "did:web:carol.test", hidden: false });
  expect((await requests()).at(-1)).toEqual({ seq: 3, did: "did:web:carol.test" });
});

test("a control write that fails fails the request before Turso is written", async () => {
  const broken = await controlStore();
  await broken.d1.exec("DROP TABLE index_requests");
  upstreams([{ did: "did:plc:dave", handle: "dave.test" }]);
  // SAFETY: as `env` above.
  const failing = { CONTROL_DB: broken.d1, KV: kv } as Env;
  try {
    await expect(handleRequestIndexing(post("?handle=dave.test"), turso, failing, background().ctx)).rejects.toThrow();
  } finally {
    await broken.dispose();
  }
  expect(await stored("did:plc:dave")).toBeNull();
});

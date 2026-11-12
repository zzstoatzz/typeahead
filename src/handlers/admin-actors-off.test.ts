import { afterAll, afterEach, expect, test } from "bun:test";
import { controlStore } from "../control-d1-fixture";
import { indexRequestsAfter, policySnapshot } from "../control-store";
import type { SqlDb } from "../db";
import { BSKY_MOD_DID } from "../moderation";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import {
  handleDomainBlockDelete, handleDomainBlockSet, handleModOverrideDelete, handleModOverrideSet, handleRequestIndexing,
} from "./admin";

// With TURSO_ACTORS=off the admin policy endpoints and /request-indexing
// write the control database only. Their responses are unchanged; the one
// value that came from a Turso row, the `hidden` a cleared override leaves
// behind, is computed from the routed slot's copy of the account.

const { d1, control, dispose } = await controlStore();
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
afterAll(dispose);

const ADMIN = "admin-secret";
const noTurso: SqlDb = {
  prepare() { throw new Error("Turso was read"); },
  batch() { throw new Error("Turso was read"); },
};

function held(did: string, handle: string, labels: SlotActor["labels"] = []): SlotActor {
  return {
    did, handle, display_name: "", avatar_url: "", labels, created_at: "", associated: {}, pds: "", description: "", banner_url: "",
    followers_count: 0, follows_count: 0, posts_count: 0, quality_score: 0, updated_at: 0, profile_checked: false, hidden: false, hidden_stored: false,
  };
}
const SLOT = [
  held("did:plc:plain", "plain.test"),
  held("did:plc:labelled", "labelled.test", [{ src: BSKY_MOD_DID, val: "!takedown", uri: "did:plc:labelled", cts: "2026-01-01T00:00:00Z" }]),
  held("did:plc:sub", "a.Spam.Example"),
];
let slotUp = true;

const kv = {
  async get(_key: string): Promise<string | null> { return "1"; },
  async put(_key: string, _value: string): Promise<void> {},
};
const transport = {
  async fetch(input: RequestInfo | URL): Promise<Response> {
    if (!slotUp) return Response.json({ error: "not ready" }, { status: 503 });
    const wanted = new URL(String(input)).searchParams.getAll("actor");
    return Response.json({ actors: SLOT.filter((actor) => wanted.includes(actor.did)) });
  },
};
// SAFETY: these handlers read ADMIN_SECRET, TURSO_ACTORS and CONTROL_DB,
// reach the slot through SEARCH_TRANSPORT.fetch, and use KV only for the
// relay announcement's get and put.
const env = {
  ADMIN_SECRET: ADMIN, TURSO_ACTORS: "off", CONTROL_DB: d1, KV: kv,
  SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport,
} as Env;

function background() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (task: Promise<unknown>) => { tasks.push(task); }, passThroughOnException() {} };
  // SAFETY: the handlers use the context only through waitUntil.
  return { ctx: c as ExecutionContext, tasks };
}

type Payload = { did?: string; action?: string; reason?: string; domain?: string };
const send = (method: string, path: string, payload?: Payload) =>
  new Request(`https://typeahead.waow.tech${path}`, {
    method,
    headers: { Authorization: `Bearer ${ADMIN}`, "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
const body = async (res: Response) => JSON.parse(await res.text());
const policy = async () => {
  const { overrides, blocked_domains } = await policySnapshot(control);
  return { overrides, blocked_domains };
};

test("policy changes are written to the control database alone and answered as before", async () => {
  const set = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide", reason: "spam ring" }), noTurso, env);
  expect(await body(set)).toEqual({ did: "did:plc:plain", action: "hide", reason: "spam ring", hidden: 1 });

  const { ctx, tasks } = background();
  const blocked = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "*.Spam.Example", reason: "wave" }), noTurso, env, ctx);
  expect(await body(blocked)).toEqual({ domain: "spam.example", reason: "wave", status: "blocked; backfilling existing matches in background" });
  expect(tasks).toEqual([]);
  expect(await policy()).toEqual({ overrides: [{ did: "did:plc:plain", action: "hide" }], blocked_domains: ["spam.example"] });
});

test("clearing an override answers with the hidden the slot's copy of the account then has", async () => {
  for (const [did, hidden] of [["did:plc:plain", 0], ["did:plc:labelled", 1], ["did:plc:sub", 1], ["did:plc:unknown", 0]] as const) {
    const res = await handleModOverrideDelete(send("DELETE", `/admin/mod-override?did=${did}`), noTurso, env);
    expect(await body(res)).toEqual({ did, hidden });
  }
  expect((await policy()).overrides).toEqual([]);
});

test("a slot that does not answer after the override is cleared is a 503 that says what happened", async () => {
  await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "show" }), noTurso, env);
  slotUp = false;
  try {
    const res = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:plain"), noTurso, env);
    expect(res.status).toBe(503);
    expect(await body(res)).toEqual({ error: "the override was cleared; the account's visibility could not be read from the slot", did: "did:plc:plain" });
    expect((await policy()).overrides).toEqual([]);
  } finally {
    slotUp = true;
  }
});

test("unblocking a domain touches the control database alone", async () => {
  const { ctx, tasks } = background();
  const res = await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=spam.example"), noTurso, env, ctx);
  expect(await body(res)).toEqual({ domain: "spam.example", status: "unblocked; recomputing affected actors in background" });
  expect(tasks).toEqual([]);
  expect((await policy()).blocked_domains).toEqual([]);
});

test("/request-indexing logs the request for the slots and answers without writing Turso", async () => {
  const reply = <T extends object>(value: T) => Response.json(value);
  const upstream = async (input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.hostname === "slingshot.microcosm.blue") return reply({ did: "did:plc:alice", handle: "alice.spam.example", pds: "https://pds.test" });
    if (url.hostname === "plc.directory") return reply({ id: "did:plc:alice", alsoKnownAs: ["at://alice.spam.example"] });
    if (url.hostname === "cloudflare-dns.com") return reply({ Status: 0, Answer: [{ type: 16, data: '"did=did:plc:alice"' }] });
    if (url.hostname === "public.api.bsky.app") return reply({ did: "did:plc:alice", handle: "alice.spam.example", labels: [] });
    return new Response("not found", { status: 404 });
  };
  globalThis.fetch = Object.assign(upstream, { preconnect: realFetch.preconnect });
  const request = () => new Request("https://typeahead.waow.tech/request-indexing?handle=alice.spam.example", { method: "POST" });

  const shown = background();
  const res = await handleRequestIndexing(request(), noTurso, env, shown.ctx);
  expect(res.status).toBe(200);
  expect(await body(res)).toEqual({ handle: "alice.spam.example", did: "did:plc:alice", hidden: false });
  expect((await indexRequestsAfter(control, 0, 100)).requests.map((r) => r.did)).toEqual(["did:plc:alice"]);
  expect(res.headers.get("Server-Timing")).not.toContain("turso_write");

  await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "spam.example" }), noTurso, env, background().ctx);
  const hidden = await handleRequestIndexing(request(), noTurso, env, background().ctx);
  expect(await body(hidden)).toEqual({ handle: "alice.spam.example", did: "did:plc:alice", hidden: true, reason: "hidden by moderation" });
});

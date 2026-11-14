import { afterAll, expect, test } from "bun:test";
import { GENERATION, controlStore } from "../control-d1-fixture";
import { policySnapshot } from "../control-store";
import { BSKY_MOD_DID } from "../moderation";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import {
  handleDomainBlockDelete, handleDomainBlockList, handleDomainBlockSet,
  handleModOverrideDelete, handleModOverrideList, handleModOverrideSet,
} from "./admin";

// The admin policy endpoints write the control database, which store slots
// pull. The one value in their answers that is about an account, the `hidden`
// a cleared override leaves behind, is computed from the routed slot's copy
// of it.

const { d1, control, dispose } = await controlStore();
afterAll(dispose);

const ADMIN = "admin-secret";

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
let slotAsked = 0;
const transport = {
  async fetch(input: RequestInfo | URL): Promise<Response> {
    slotAsked++;
    if (!slotUp) return Response.json({ error: "not ready" }, { status: 503 });
    const wanted = new URL(String(input)).searchParams.getAll("actor");
    return Response.json({ actors: SLOT.filter((actor) => wanted.includes(actor.did)) });
  },
};
// SAFETY: the policy handlers read ADMIN_SECRET and CONTROL_DB, and reach
// the slot only through SEARCH_BACKEND_URL and SEARCH_TRANSPORT.fetch.
const env = { ADMIN_SECRET: ADMIN, CONTROL_DB: d1, SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport } as Env;

type Payload = { did?: string; action?: string; reason?: string; domain?: string };
const send = (method: string, path: string, payload?: Payload, auth = `Bearer ${ADMIN}`) =>
  new Request(`https://typeahead.waow.tech${path}`, {
    method,
    headers: { Authorization: auth, "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
const body = async (res: Response) => JSON.parse(await res.text());

test("setting an override writes the control record and answers with the hidden it forces", async () => {
  const res = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide", reason: "spam ring" }), env);
  expect(res.status).toBe(200);
  expect(await res.text()).toBe('{"did":"did:plc:plain","action":"hide","reason":"spam ring","hidden":1}');
  expect(await policySnapshot(control)).toEqual({ generation: GENERATION, revision: 2, overrides: [{ did: "did:plc:plain", action: "hide" }], blocked_domains: [] });

  const shown = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:labelled", action: "show" }), env);
  expect(await shown.text()).toBe('{"did":"did:plc:labelled","action":"show","reason":"","hidden":0}');
  expect((await policySnapshot(control)).overrides).toEqual([
    { did: "did:plc:labelled", action: "show" }, { did: "did:plc:plain", action: "hide" },
  ]);
  expect(slotAsked).toBe(0);
});

test("the override listing reads the control record", async () => {
  const res = await handleModOverrideList(send("GET", "/admin/mod-overrides"), env);
  const listed = await body(res);
  expect(listed.overrides.map((row: { did: string }) => row.did).sort()).toEqual(["did:plc:labelled", "did:plc:plain"]);
  expect(listed.overrides.find((row: { did: string }) => row.did === "did:plc:plain")).toMatchObject({ action: "hide", reason: "spam ring" });
  for (const row of listed.overrides) {
    expect(Object.keys(row).sort()).toEqual(["action", "created_at", "did", "reason"]);
    expect(row.created_at).toBeGreaterThan(1_700_000_000);
  }
});

test("blocking a domain writes the control record, normalized, and lists it", async () => {
  const res = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "*.Spam.Example", reason: "farm" }), env);
  expect(res.status).toBe(200);
  expect(await res.text()).toBe('{"domain":"spam.example","reason":"farm","status":"blocked; slots apply it on their next policy pull"}');
  expect((await policySnapshot(control)).blocked_domains).toEqual(["spam.example"]);

  const listed = await body(await handleDomainBlockList(send("GET", "/admin/domain-blocks"), env));
  expect(listed.blocked_domains).toHaveLength(1);
  expect(listed.blocked_domains[0]).toMatchObject({ domain: "spam.example", reason: "farm" });
  expect(Object.keys(listed.blocked_domains[0]).sort()).toEqual(["created_at", "domain", "reason"]);
  expect(slotAsked).toBe(0);
});

test("clearing an override answers with the hidden the slot's copy of the account then has", async () => {
  // no label, a takedown label, a handle under the blocked domain, an account the slot does not hold
  for (const [did, hidden] of [["did:plc:plain", 0], ["did:plc:labelled", 1], ["did:plc:sub", 1], ["did:plc:unknown", 0]] as const) {
    const res = await handleModOverrideDelete(send("DELETE", `/admin/mod-override?did=${did}`), env);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(JSON.stringify({ did, hidden }));
  }
  expect(await policySnapshot(control)).toEqual({ generation: GENERATION, revision: 8, overrides: [], blocked_domains: ["spam.example"] });
});

test("a slot that does not answer after the override is cleared is a 503 that says what happened", async () => {
  await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "show" }), env);
  slotUp = false;
  try {
    const res = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:plain"), env);
    expect(res.status).toBe(503);
    expect(await body(res)).toEqual({ error: "the override was cleared; the account's visibility could not be read from the slot", did: "did:plc:plain" });
    expect((await policySnapshot(control)).overrides).toEqual([]);
  } finally {
    slotUp = true;
  }
});

test("unblocking a domain removes it from the control record", async () => {
  const res = await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=.SPAM.example"), env);
  expect(res.status).toBe(200);
  expect(await res.text()).toBe('{"domain":"spam.example","status":"unblocked; slots apply it on their next policy pull"}');
  expect((await policySnapshot(control)).blocked_domains).toEqual([]);

  const unblocked = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:sub"), env);
  expect(await body(unblocked)).toEqual({ did: "did:plc:sub", hidden: 0 });
});

test("a refused request changes nothing", async () => {
  await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:rescued", action: "show" }), env);
  const before = await policySnapshot(control);
  const asked = slotAsked;
  const refused = [
    await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide" }, "Bearer wrong"), env),
    await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:rescued", undefined, "Bearer wrong"), env),
    await handleModOverrideList(send("GET", "/admin/mod-overrides", undefined, "Bearer wrong"), env),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "spam.example" }, "Bearer wrong"), env),
    await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=spam.example", undefined, "Bearer wrong"), env),
    await handleDomainBlockList(send("GET", "/admin/domain-blocks", undefined, "Bearer wrong"), env),
  ];
  for (const res of refused) {
    expect(res.status).toBe(401);
    expect(await body(res)).toEqual({ error: "unauthorized" });
  }

  const invalid = [
    await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "delete" }), env),
    await handleModOverrideSet(send("POST", "/admin/mod-override", { action: "hide" }), env),
    await handleModOverrideDelete(send("DELETE", "/admin/mod-override"), env),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "nodot" }), env),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "has space.example" }), env),
    await handleDomainBlockDelete(send("DELETE", "/admin/domain-block"), env),
  ];
  for (const res of invalid) expect(res.status).toBe(400);

  expect(await policySnapshot(control)).toEqual(before);
  expect(before.overrides).toEqual([{ did: "did:plc:rescued", action: "show" }]);
  expect(slotAsked).toBe(asked);
});

test("a subject a slot would refuse is refused at the write, so it is never served", async () => {
  const before = await policySnapshot(control);
  const dids = ["alice.bsky.social", " did:plc:63hvnyjvqi2nzzcsjgnry5we", "did:plc:63hvnyjvqi2nzzcsjgnry5we ", "did:plc:" + "a".repeat(2100), "did:plc:a\u0000b", "at://did:plc:63hvnyjvqi2nzzcsjgnry5we"];
  for (const did of dids) {
    const res = await handleModOverrideSet(send("POST", "/admin/mod-override", { did, action: "hide" }), env);
    expect([did.slice(0, 40), res.status]).toEqual([did.slice(0, 40), 400]);
  }
  const domains = ["a\u0001b.example", "x".repeat(300) + ".example", "a\u007f.example", "caf\u00e9.example"];
  for (const domain of domains) {
    const res = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain }), env);
    expect([domain.slice(0, 40), res.status]).toEqual([domain.slice(0, 40), 400]);
  }
  expect(await policySnapshot(control)).toEqual(before);

  // what an operator sends today still goes through
  const link = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:63hvnyjvqi2nzzcsjgnry5we", action: "show", reason: "FREE LINK" }), env);
  expect(link.status).toBe(200);
  const real = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "dadavidtseng.com" }), env);
  expect(real.status).toBe(200);
  expect(await policySnapshot(control)).toMatchObject({
    overrides: [{ did: "did:plc:63hvnyjvqi2nzzcsjgnry5we", action: "show" }, { did: "did:plc:rescued", action: "show" }],
    blocked_domains: ["dadavidtseng.com"],
  });
  await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:63hvnyjvqi2nzzcsjgnry5we"), env);
  await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=dadavidtseng.com"), env);
  expect(await policySnapshot(control)).toMatchObject({ overrides: before.overrides, blocked_domains: before.blocked_domains });
});

test("with the control database out of reach an admin change answers 503 and changes nothing", async () => {
  const before = await policySnapshot(control);
  const asked = slotAsked;
  const unmigrated = await controlStore();
  for (const table of ["mod_overrides", "blocked_domains", "control_meta", "index_requests"]) await unmigrated.d1.exec(`DROP TABLE ${table}`);
  // SAFETY: as `env` above; the second has no control binding at all.
  const failing = [{ ...env, CONTROL_DB: unmigrated.d1 } as Env, { ADMIN_SECRET: ADMIN, SEARCH_BACKEND_URL: env.SEARCH_BACKEND_URL, SEARCH_TRANSPORT: transport } as Env];
  try {
    for (const down of failing) {
      const answers = [
        await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide" }), down),
        await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:rescued"), down),
        await handleModOverrideList(send("GET", "/admin/mod-overrides"), down),
        await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "spam.example" }), down),
        await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=spam.example"), down),
        await handleDomainBlockList(send("GET", "/admin/domain-blocks"), down),
      ];
      for (const res of answers) {
        expect(res.status).toBe(503);
        expect((await body(res)).error).toBe("the control database is unavailable; nothing was changed");
      }
    }
  } finally {
    await unmigrated.dispose();
  }
  expect(await policySnapshot(control)).toEqual(before);
  expect(slotAsked).toBe(asked);
});

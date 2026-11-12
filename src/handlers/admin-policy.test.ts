import { afterAll, expect, test } from "bun:test";
import { GENERATION, controlStore, tursoPolicyTables } from "../control-d1-fixture";
import { policySnapshot } from "../control-store";
import { BSKY_MOD_DID } from "../moderation";
import type { Env } from "../types";
import {
  handleDomainBlockDelete, handleDomainBlockList, handleDomainBlockSet,
  handleModOverrideDelete, handleModOverrideList, handleModOverrideSet,
} from "./admin";

// The admin policy endpoints write the control database, which store slots
// pull, and then Turso, which the Turso-fed slots still read. Their responses
// are the ones they gave when Turso was the only store.

const { d1, control, dispose: disposeControl } = await controlStore();
const { turso, dispose: disposeTurso } = await tursoPolicyTables();
afterAll(async () => {
  disposeTurso();
  await disposeControl();
});

const ADMIN = "admin-secret";
// SAFETY: the policy handlers read only ADMIN_SECRET and CONTROL_DB.
const env = { ADMIN_SECRET: ADMIN, CONTROL_DB: d1 } as Env;

function background() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (task: Promise<unknown>) => { tasks.push(task); }, passThroughOnException() {} };
  // SAFETY: the handlers use the context only through waitUntil.
  return { ctx: c as ExecutionContext, tasks };
}

type Payload = { did?: string; action?: string; reason?: string; domain?: string };
const send = (method: string, path: string, payload?: Payload, auth = `Bearer ${ADMIN}`) =>
  new Request(`https://typeahead.waow.tech${path}`, {
    method,
    headers: { Authorization: auth, "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
const body = async (res: Response) => JSON.parse(await res.text());

const tursoOverrides = async () =>
  (await turso.prepare("SELECT did, action, reason FROM mod_overrides ORDER BY did").all<{ did: string; action: string; reason: string }>()).results.map((row) => ({ ...row }));
const tursoDomains = async () =>
  (await turso.prepare("SELECT domain, reason FROM blocked_domains ORDER BY domain").all<{ domain: string; reason: string }>()).results.map((row) => ({ ...row }));
const hidden = async (did: string) =>
  (await turso.prepare("SELECT hidden FROM actors WHERE did = ?1").bind(did).first<{ hidden: number }>())?.hidden;

const TAKEN_DOWN = JSON.stringify([{ src: BSKY_MOD_DID, val: "!takedown", uri: "did:plc:labelled", cts: "2026-01-01T00:00:00Z" }]);
await turso.batch([
  turso.prepare("INSERT INTO actors (did, handle) VALUES ('did:plc:plain', 'plain.test')"),
  turso.prepare("INSERT INTO actors (did, handle, labels, hidden) VALUES ('did:plc:labelled', 'labelled.test', ?1, 1)").bind(TAKEN_DOWN),
  turso.prepare("INSERT INTO actors (did, handle) VALUES ('did:plc:apex', 'spam.example')"),
  turso.prepare("INSERT INTO actors (did, handle) VALUES ('did:plc:sub', 'a.Spam.Example')"),
  turso.prepare("INSERT INTO actors (did, handle) VALUES ('did:plc:rescued', 'ok.spam.example')"),
  turso.prepare("INSERT INTO actors (did, handle) VALUES ('did:plc:other', 'notspam.example')"),
]);

test("setting an override writes the control record and Turso, and answers as before", async () => {
  const res = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide", reason: "spam ring" }), turso, env);
  expect(res.status).toBe(200);
  expect(await body(res)).toEqual({ did: "did:plc:plain", action: "hide", reason: "spam ring", hidden: 1 });
  expect(await policySnapshot(control)).toEqual({ generation: GENERATION, revision: 2, overrides: [{ did: "did:plc:plain", action: "hide" }], blocked_domains: [] });
  expect(await tursoOverrides()).toEqual([{ did: "did:plc:plain", action: "hide", reason: "spam ring" }]);
  expect(await hidden("did:plc:plain")).toBe(1);

  const shown = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:labelled", action: "show" }), turso, env);
  expect(await body(shown)).toEqual({ did: "did:plc:labelled", action: "show", reason: "", hidden: 0 });
  expect(await hidden("did:plc:labelled")).toBe(0);
  expect((await policySnapshot(control)).overrides).toEqual([
    { did: "did:plc:labelled", action: "show" }, { did: "did:plc:plain", action: "hide" },
  ]);
});

test("the override listing reads the control record in its old shape", async () => {
  const res = await handleModOverrideList(send("GET", "/admin/mod-overrides"), env);
  const listed = await body(res);
  expect(listed.overrides.map((row: { did: string }) => row.did).sort()).toEqual(["did:plc:labelled", "did:plc:plain"]);
  for (const row of listed.overrides) {
    expect(Object.keys(row).sort()).toEqual(["action", "created_at", "did", "reason"]);
    expect(row.created_at).toBeGreaterThan(1_700_000_000);
  }
});

test("clearing an override removes it from both and recomputes what remains", async () => {
  const res = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:labelled"), turso, env);
  expect(await body(res)).toEqual({ did: "did:plc:labelled", hidden: 1 });
  expect(await hidden("did:plc:labelled")).toBe(1);

  const plain = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:plain"), turso, env);
  expect(await body(plain)).toEqual({ did: "did:plc:plain", hidden: 0 });
  expect(await tursoOverrides()).toEqual([]);
  expect(await policySnapshot(control)).toEqual({ generation: GENERATION, revision: 5, overrides: [], blocked_domains: [] });

  const unknown = await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:unknown"), turso, env);
  expect(await body(unknown)).toEqual({ did: "did:plc:unknown", hidden: 0 });
});

test("blocking a domain writes both stores and hides its stored actors in the background", async () => {
  await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:rescued", action: "show" }), turso, env);
  const { ctx, tasks } = background();
  const res = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "*.Spam.Example", reason: "farm" }), turso, env, ctx);
  expect(await body(res)).toEqual({ domain: "spam.example", reason: "farm", status: "blocked; backfilling existing matches in background" });
  expect((await policySnapshot(control)).blocked_domains).toEqual(["spam.example"]);
  expect(await tursoDomains()).toEqual([{ domain: "spam.example", reason: "farm" }]);
  await Promise.all(tasks);
  expect(await hidden("did:plc:apex")).toBe(1);
  expect(await hidden("did:plc:sub")).toBe(1);
  expect(await hidden("did:plc:rescued")).toBe(0);
  expect(await hidden("did:plc:other")).toBe(0);

  const listed = await body(await handleDomainBlockList(send("GET", "/admin/domain-blocks"), env));
  expect(listed.blocked_domains).toHaveLength(1);
  expect(listed.blocked_domains[0]).toMatchObject({ domain: "spam.example", reason: "farm" });
  expect(Object.keys(listed.blocked_domains[0]).sort()).toEqual(["created_at", "domain", "reason"]);
});

test("unblocking a domain removes it from both stores and restores its actors", async () => {
  const { ctx, tasks } = background();
  const res = await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=.SPAM.example"), turso, env, ctx);
  expect(await body(res)).toEqual({ domain: "spam.example", status: "unblocked; recomputing affected actors in background" });
  expect((await policySnapshot(control)).blocked_domains).toEqual([]);
  expect(await tursoDomains()).toEqual([]);
  await Promise.all(tasks);
  expect(await hidden("did:plc:apex")).toBe(0);
  expect(await hidden("did:plc:sub")).toBe(0);
});

test("a refused request changes neither store", async () => {
  const before = await policySnapshot(control);
  const { ctx, tasks } = background();
  const refused = [
    await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide" }, "Bearer wrong"), turso, env),
    await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:rescued", undefined, "Bearer wrong"), turso, env),
    await handleModOverrideList(send("GET", "/admin/mod-overrides", undefined, "Bearer wrong"), env),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "spam.example" }, "Bearer wrong"), turso, env, ctx),
    await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=spam.example", undefined, "Bearer wrong"), turso, env, ctx),
    await handleDomainBlockList(send("GET", "/admin/domain-blocks", undefined, "Bearer wrong"), env),
  ];
  for (const res of refused) expect(res.status).toBe(401);

  const invalid = [
    await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "delete" }), turso, env),
    await handleModOverrideSet(send("POST", "/admin/mod-override", { action: "hide" }), turso, env),
    await handleModOverrideDelete(send("DELETE", "/admin/mod-override"), turso, env),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "nodot" }), turso, env, ctx),
    await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "has space.example" }), turso, env, ctx),
    await handleDomainBlockDelete(send("DELETE", "/admin/domain-block"), turso, env, ctx),
  ];
  for (const res of invalid) expect(res.status).toBe(400);

  expect(tasks).toEqual([]);
  expect(await policySnapshot(control)).toEqual(before);
  expect(await tursoOverrides()).toEqual([{ did: "did:plc:rescued", action: "show", reason: "" }]);
  expect(await tursoDomains()).toEqual([]);
});

test("a subject a slot would refuse is refused at the write, so it is never served", async () => {
  const before = await policySnapshot(control);
  const { ctx, tasks } = background();
  const dids = ["alice.bsky.social", " did:plc:63hvnyjvqi2nzzcsjgnry5we", "did:plc:63hvnyjvqi2nzzcsjgnry5we ", "did:plc:" + "a".repeat(2100), "did:plc:a\u0000b", "at://did:plc:63hvnyjvqi2nzzcsjgnry5we"];
  for (const did of dids) {
    const res = await handleModOverrideSet(send("POST", "/admin/mod-override", { did, action: "hide" }), turso, env);
    expect([did.slice(0, 40), res.status]).toEqual([did.slice(0, 40), 400]);
  }
  const domains = ["a\u0001b.example", "x".repeat(300) + ".example", "a\u007f.example", "caf\u00e9.example"];
  for (const domain of domains) {
    const res = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain }), turso, env, ctx);
    expect([domain.slice(0, 40), res.status]).toEqual([domain.slice(0, 40), 400]);
  }
  expect(tasks).toEqual([]);
  expect(await policySnapshot(control)).toEqual(before);
  expect(await tursoOverrides()).toEqual([{ did: "did:plc:rescued", action: "show", reason: "" }]);
  expect(await tursoDomains()).toEqual([]);

  // what an operator sends today still goes through
  const link = await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:63hvnyjvqi2nzzcsjgnry5we", action: "show", reason: "FREE LINK" }), turso, env);
  expect(link.status).toBe(200);
  const real = await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "dadavidtseng.com" }), turso, env, ctx);
  expect(real.status).toBe(200);
  await Promise.all(tasks);
  await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:63hvnyjvqi2nzzcsjgnry5we"), turso, env);
  await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=dadavidtseng.com"), turso, env, ctx);
  await Promise.all(tasks);
});

test("with the control database out of reach an admin change answers 503 and changes nothing", async () => {
  const unmigrated = await controlStore();
  for (const table of ["mod_overrides", "blocked_domains", "control_meta", "index_requests"]) await unmigrated.d1.exec(`DROP TABLE ${table}`);
  // SAFETY: as `env` above; the second has no binding at all.
  const failing = [{ ADMIN_SECRET: ADMIN, CONTROL_DB: unmigrated.d1 } as Env, { ADMIN_SECRET: ADMIN } as Env];
  const { ctx, tasks } = background();
  try {
    for (const down of failing) {
      const answers = [
        await handleModOverrideSet(send("POST", "/admin/mod-override", { did: "did:plc:plain", action: "hide" }), turso, down),
        await handleModOverrideDelete(send("DELETE", "/admin/mod-override?did=did:plc:rescued"), turso, down),
        await handleModOverrideList(send("GET", "/admin/mod-overrides"), down),
        await handleDomainBlockSet(send("POST", "/admin/domain-block", { domain: "spam.example" }), turso, down, ctx),
        await handleDomainBlockDelete(send("DELETE", "/admin/domain-block?domain=spam.example"), turso, down, ctx),
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
  expect(tasks).toEqual([]);
  expect(await tursoOverrides()).toEqual([{ did: "did:plc:rescued", action: "show", reason: "" }]);
  expect(await tursoDomains()).toEqual([]);
  expect(await hidden("did:plc:plain")).toBe(0);
});

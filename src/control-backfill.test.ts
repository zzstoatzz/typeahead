import { afterAll, expect, test } from "bun:test";
import { controlStore } from "./control-d1-fixture";
import { CONTROL_QUERIES, backfillSql, domainRow, overrideRow, parseWranglerRows, policyDifferences, type Policy } from "./control-backfill";
import { policySnapshot, setOverride, unblockDomain } from "./control-store";

const { d1, control, dispose } = await controlStore();
afterAll(dispose);

// production's rows on 2026-11-12, in shape: one override and three blocked
// domains, with reasons, one of them holding a quote
const TURSO: Policy = {
  overrides: [{ did: "did:plc:rescued", action: "show", reason: "false positive on a blocked domain", at: 1_780_000_000 }],
  domains: [
    { domain: "farm.example", reason: "it's a handle farm", at: 1_781_000_000 },
    { domain: "Mixed.Example", reason: "", at: 1_782_000_000 },
    { domain: "spam.example", reason: "spam ring", at: 1_783_000_000 },
  ],
};

async function apply(sql: string): Promise<void> {
  const statements = sql.split("\n").filter(Boolean);
  if (statements.length > 0) await d1.batch(statements.map((statement) => d1.prepare(statement)));
}

async function held(): Promise<Policy> {
  const [overrides, domains] = await Promise.all([
    control.prepare(CONTROL_QUERIES.overrides).all(),
    control.prepare(CONTROL_QUERIES.domains).all(),
  ]);
  return { overrides: overrideRow.array().parse(overrides.results), domains: domainRow.array().parse(domains.results) };
}

test("the backfill carries every rule with its reason and time, under a new revision", async () => {
  expect(policyDifferences(TURSO, await held())).toHaveLength(4);
  await apply(backfillSql(TURSO));
  expect(await held()).toEqual({
    overrides: TURSO.overrides,
    domains: [
      { domain: "farm.example", reason: "it's a handle farm", at: 1_781_000_000 },
      { domain: "mixed.example", reason: "", at: 1_782_000_000 },
      { domain: "spam.example", reason: "spam ring", at: 1_783_000_000 },
    ],
  });
  expect(policyDifferences(TURSO, await held())).toEqual([]);
  expect(await policySnapshot(control)).toEqual({
    revision: 3,
    overrides: [{ did: "did:plc:rescued", action: "show" }],
    blocked_domains: ["farm.example", "mixed.example", "spam.example"],
  });
});

test("running it again changes no rule and never lowers the revision", async () => {
  const before = await held();
  await apply(backfillSql(TURSO));
  expect(await held()).toEqual(before);
  expect((await policySnapshot(control)).revision).toBe(5);
});

test("a rule the control database already changed is left as it is", async () => {
  await setOverride(control, "did:plc:rescued", "hide", "changed since", 1_794_000_000);
  await apply(backfillSql(TURSO));
  expect((await held()).overrides).toEqual([{ did: "did:plc:rescued", action: "hide", reason: "changed since", at: 1_794_000_000 }]);
});

test("the check names each disagreement", async () => {
  await unblockDomain(control, "spam.example");
  await control.prepare("INSERT INTO blocked_domains (domain, reason, updated_at) VALUES ('extra.example', '', 1)").run();
  await control.prepare("UPDATE blocked_domains SET reason = 'edited' WHERE domain = 'farm.example'").run();
  expect(policyDifferences(TURSO, await held())).toEqual([
    "override did:plc:rescued: show in Turso, hide in control",
    "domain farm.example: reason differs",
    "domain spam.example: blocked in Turso, absent from control",
    "domain extra.example: blocked in control, absent from Turso",
  ]);
  expect(policyDifferences({ overrides: [], domains: [] }, { overrides: TURSO.overrides, domains: [] })).toEqual([
    "override did:plc:rescued: show in control, absent from Turso",
  ]);
});

test("an empty Turso policy needs no statement", () => {
  expect(backfillSql({ overrides: [], domains: [] })).toBe("");
});

test("rows that are not policy are refused rather than carried", () => {
  expect(() => overrideRow.parse({ did: "did:plc:x", action: "delete", reason: "", at: 1 })).toThrow();
  expect(() => domainRow.parse({ domain: "", reason: "", at: 1 })).toThrow();
  expect(overrideRow.parse({ did: "did:plc:x", action: "hide", reason: null, at: 1 }).reason).toBe("");
});

test("a Turso rule a slot would refuse stops the backfill and is named", () => {
  const bad = (did: string): Policy => ({ overrides: [...TURSO.overrides, { did, action: "hide", reason: "", at: 1 }], domains: [] });
  expect(() => backfillSql(bad("alice.bsky.social"))).toThrow('Turso override "alice.bsky.social"');
  expect(() => backfillSql(bad(" did:plc:63hvnyjvqi2nzzcsjgnry5we"))).toThrow("is not a DID a slot accepts");
  expect(() => backfillSql(bad("did:plc:" + "a".repeat(2100)))).toThrow("Turso override");
  const blocked = (domain: string): Policy => ({ overrides: [], domains: [...TURSO.domains, { domain, reason: "", at: 1 }] });
  expect(() => backfillSql(blocked("a\u0001b.example"))).toThrow("Turso blocked domain");
  expect(() => backfillSql(blocked("x".repeat(300) + ".example"))).toThrow("is not a domain a slot accepts");
  expect(() => backfillSql(blocked("nodot"))).toThrow('Turso blocked domain "nodot"');
});

test("wrangler's JSON output is read as one statement's rows", () => {
  const output = JSON.stringify([{ results: [{ domain: "spam.example", reason: "r", at: 5 }], success: true, meta: { duration: 0 } }]);
  expect(parseWranglerRows(output, domainRow)).toEqual([{ domain: "spam.example", reason: "r", at: 5 }]);
  expect(() => parseWranglerRows("[]", domainRow)).toThrow();
  expect(() => parseWranglerRows(JSON.stringify([{ results: [{ domain: 5 }] }]), domainRow)).toThrow();
});

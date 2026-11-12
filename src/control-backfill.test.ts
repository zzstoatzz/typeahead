import { afterAll, expect, test } from "bun:test";
import { GENERATION, controlStore } from "./control-d1-fixture";
import { CONTROL_QUERIES, META_QUERY, backfillSql, domainRow, overrideRow, parseWranglerRows, policyDifferences, schemaProblem, servedDifferences, servedPolicy, type Policy } from "./control-backfill";
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
    generation: GENERATION,
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

test("the deploy gate's schema probe tells a migrated database from one that is not", async () => {
  const rows = (await control.prepare(META_QUERY).all()).results;
  expect(schemaProblem(JSON.stringify([{ results: rows, success: true }]))).toBeNull();
  // only the first migration applied
  expect(schemaProblem(JSON.stringify([{ results: [{ name: "policy_revision", value: 1 }] }]))).toBe("control_meta has no generation; apply migrations/control-d1");
  expect(schemaProblem(JSON.stringify([{ results: [] }]))).toContain("control_meta has no generation");
  // no schema at all: wrangler fails the statement and prints an error
  for (const output of ["", "✘ [ERROR] no such table: control_meta: SQLITE_ERROR", "wrangler d1 execute exited 1: Couldn't find a D1 DB"]) {
    expect(schemaProblem(output)).toContain("apply migrations/control-d1");
  }
});

test("the slot gate compares what is served with Turso's rules", () => {
  const served = {
    generation: 9, revision: 3,
    overrides: [{ did: "did:plc:rescued", action: "show" as const }],
    blocked_domains: ["farm.example", "mixed.example", "spam.example"],
  };
  expect(servedDifferences(TURSO, served)).toEqual([]);
  // an unbackfilled database: every rule Turso holds is one a slot would drop
  expect(servedDifferences(TURSO, { generation: 9, revision: 1, overrides: [], blocked_domains: [] })).toEqual([
    "override did:plc:rescued: show in Turso, not served",
    "domain farm.example: blocked in Turso, not served",
    "domain mixed.example: blocked in Turso, not served",
    "domain spam.example: blocked in Turso, not served",
  ]);
  expect(servedDifferences(TURSO, { ...served, overrides: [{ did: "did:plc:rescued", action: "hide" }, { did: "did:plc:extra", action: "hide" }], blocked_domains: ["farm.example", "spam.example", "more.example"] })).toEqual([
    "override did:plc:rescued: show in Turso, hide served",
    "override did:plc:extra: hide served, absent from Turso",
    "domain mixed.example: blocked in Turso, not served",
    "domain more.example: served, absent from Turso",
  ]);
  expect(() => servedPolicy.parse({ revision: 1, overrides: [], blocked_domains: [] })).toThrow();
});

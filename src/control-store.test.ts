import { afterAll, expect, test } from "bun:test";
import { controlStore } from "./control-d1-fixture";
import {
  INDEX_REQUEST_REPEAT_SECONDS, INDEX_REQUEST_RETENTION_SECONDS,
  blockDomain, clearOverride, indexRequestsAfter, listBlockedDomains, listOverrides,
  policyRevision, policySnapshot, pruneIndexRequests, recordIndexRequest, setOverride, unblockDomain,
} from "./control-store";

const { control, dispose } = await controlStore();
afterAll(dispose);

const NOW = 1_794_400_000;

test("a migrated database serves an empty policy at revision 1", async () => {
  expect(await policySnapshot(control)).toEqual({ revision: 1, overrides: [], blocked_domains: [] });
  expect(await indexRequestsAfter(control, 0, 10)).toEqual({ requests: [], more: false });
});

test("every policy change raises the revision, removals included", async () => {
  await setOverride(control, "did:plc:hidden", "hide", "spam ring", NOW);
  expect(await policyRevision(control)).toBe(2);
  await setOverride(control, "did:plc:shown", "show", "", NOW + 1);
  await blockDomain(control, "spam.example", "operator note", NOW + 2);
  await blockDomain(control, "other.example", "", NOW + 3);
  expect(await policySnapshot(control)).toEqual({
    revision: 5,
    overrides: [{ did: "did:plc:hidden", action: "hide" }, { did: "did:plc:shown", action: "show" }],
    blocked_domains: ["other.example", "spam.example"],
  });

  await setOverride(control, "did:plc:hidden", "show", "appealed", NOW + 4);
  expect(await listOverrides(control)).toEqual([
    { did: "did:plc:hidden", action: "show", reason: "appealed", created_at: NOW + 4 },
    { did: "did:plc:shown", action: "show", reason: "", created_at: NOW + 1 },
  ]);
  expect(await listBlockedDomains(control)).toEqual([
    { domain: "other.example", reason: "", created_at: NOW + 3 },
    { domain: "spam.example", reason: "operator note", created_at: NOW + 2 },
  ]);

  await clearOverride(control, "did:plc:hidden");
  await unblockDomain(control, "spam.example");
  expect(await policySnapshot(control)).toEqual({
    revision: 8,
    overrides: [{ did: "did:plc:shown", action: "show" }],
    blocked_domains: ["other.example"],
  });

  await clearOverride(control, "did:plc:never-set");
  expect(await policyRevision(control)).toBe(9);
});

test("the snapshot carries no operator reason", async () => {
  await blockDomain(control, "noted.example", "private operator note", NOW);
  expect(JSON.stringify(await policySnapshot(control))).not.toContain("private operator note");
  await unblockDomain(control, "noted.example");
});

test("an override action outside show and hide is refused by the schema", async () => {
  const before = await policyRevision(control);
  // SAFETY: the test hands the store a value its type forbids, to reach the CHECK constraint.
  const invalid = "delete" as "hide";
  await expect(setOverride(control, "did:plc:bad", invalid, "", NOW)).rejects.toThrow();
  expect(await policyRevision(control)).toBe(before);
});

test("index requests take rising sequences and page in order", async () => {
  for (const [i, did] of ["did:plc:a", "did:plc:b", "did:plc:c"].entries()) await recordIndexRequest(control, did, NOW + i);
  expect(await indexRequestsAfter(control, 0, 2)).toEqual({
    requests: [{ seq: 1, did: "did:plc:a" }, { seq: 2, did: "did:plc:b" }],
    more: true,
  });
  expect(await indexRequestsAfter(control, 2, 2)).toEqual({ requests: [{ seq: 3, did: "did:plc:c" }], more: false });
  expect(await indexRequestsAfter(control, 3, 2)).toEqual({ requests: [], more: false });
});

test("a repeat request inside the window adds no row, and one after it does", async () => {
  await recordIndexRequest(control, "did:plc:a", NOW + INDEX_REQUEST_REPEAT_SECONDS - 1);
  expect((await indexRequestsAfter(control, 3, 10)).requests).toEqual([]);
  await recordIndexRequest(control, "did:plc:a", NOW + INDEX_REQUEST_REPEAT_SECONDS);
  expect((await indexRequestsAfter(control, 3, 10)).requests).toEqual([{ seq: 4, did: "did:plc:a" }]);
});

test("pruned sequences are never handed out again", async () => {
  const later = NOW + INDEX_REQUEST_REPEAT_SECONDS + INDEX_REQUEST_RETENTION_SECONDS + 1;
  expect(await pruneIndexRequests(control, later)).toBe(4);
  expect(await indexRequestsAfter(control, 0, 10)).toEqual({ requests: [], more: false });
  await recordIndexRequest(control, "did:plc:d", later);
  expect((await indexRequestsAfter(control, 0, 10)).requests).toEqual([{ seq: 5, did: "did:plc:d" }]);
});

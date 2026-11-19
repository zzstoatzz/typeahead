import { afterAll, expect, test } from "bun:test";
import { GENERATION, controlStore } from "./control-d1-fixture";
import {
  INDEX_REQUEST_REPEAT_SECONDS, INDEX_REQUEST_RETENTION_SECONDS,
  NEW_GENERATION_SQL, blockDomain, clearOverride, controlMeta, indexRequestPage, indexRequestsAfter, listBlockedDomains, listOverrides,
  policyRevision, policySnapshot, pruneIndexRequests, recordIndexRequest, setOverride, unblockDomain,
} from "./control-store";

const { control, dispose } = await controlStore();
afterAll(dispose);

const NOW = 1_794_400_000;

test("a migrated database serves an empty policy at revision 1", async () => {
  expect(await policySnapshot(control)).toEqual({ generation: GENERATION, revision: 1, overrides: [], blocked_domains: [] });
  expect(await indexRequestsAfter(control, 0, 10)).toEqual({ requests: [], more: false });
});

test("every policy change raises the revision, removals included", async () => {
  await setOverride(control, "did:plc:hidden", "hide", "spam ring", NOW);
  expect(await policyRevision(control)).toBe(2);
  await setOverride(control, "did:plc:shown", "show", "", NOW + 1);
  await blockDomain(control, "spam.example", "operator note", NOW + 2);
  await blockDomain(control, "other.example", "", NOW + 3);
  expect(await policySnapshot(control)).toEqual({
    generation: GENERATION,
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
    generation: GENERATION,
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

// -- a database that is recreated or restored --

test("the migration seeds a random generation a JavaScript number holds exactly", async () => {
  const a = await controlStore();
  const b = await controlStore();
  try {
    for (const seeded of [a.seededGeneration, b.seededGeneration]) {
      expect(Number.isSafeInteger(seeded)).toBe(true);
      expect(seeded).toBeGreaterThan(0);
    }
    expect(a.seededGeneration).not.toBe(b.seededGeneration);
  } finally {
    await a.dispose();
    await b.dispose();
  }
});

test("after a restore and a new generation, a slot's later sequence no longer hides requests from it", async () => {
  const store = await controlStore();
  try {
    for (const [i, did] of ["did:plc:a", "did:plc:b", "did:plc:c"].entries()) await recordIndexRequest(store.control, did, 1000 + i);
    const held = { generation: GENERATION, sequence: 3 };
    // the database goes back to when one request existed, and takes another
    await store.d1.batch([
      store.d1.prepare("DELETE FROM index_requests WHERE seq > 1"),
      store.d1.prepare("UPDATE sqlite_sequence SET seq = 1 WHERE name = 'index_requests'"),
    ]);
    await recordIndexRequest(store.control, "did:plc:new-after-restore", 5000);
    // without a new generation the slot is told there is nothing: the gap
    // this guards against
    expect((await indexRequestPage(store.control, held.generation, held.sequence, 100)).requests).toEqual([]);

    await store.d1.prepare(NEW_GENERATION_SQL).run();
    const page = await indexRequestPage(store.control, held.generation, held.sequence, 100);
    expect(page.generation).not.toBe(GENERATION);
    expect(page.requests).toEqual([{ seq: 1, did: "did:plc:a" }, { seq: 2, did: "did:plc:new-after-restore" }]);
    // once the slot holds the new generation its position counts again
    expect((await indexRequestPage(store.control, page.generation, 2, 100)).requests).toEqual([]);
    expect((await indexRequestPage(store.control, null, 2, 100)).requests).toHaveLength(2);
  } finally {
    await store.dispose();
  }
});

test("a new generation is a number a slot accepts, different each time", async () => {
  const store = await controlStore();
  try {
    const seen = new Set<number>([GENERATION]);
    for (let i = 0; i < 5; i++) {
      await store.d1.prepare(NEW_GENERATION_SQL).run();
      const { generation } = await controlMeta(store.control);
      expect(Number.isSafeInteger(generation) && generation > 0).toBe(true);
      seen.add(generation);
    }
    expect(seen.size).toBe(6);
  } finally {
    await store.dispose();
  }
});

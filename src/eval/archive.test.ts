import { expect, test } from "bun:test";
import { INDEX_KEY, publishReports, readSnapshot } from "./archive";
import { reportSchema, type Report } from "./model";

const report: Report = { version: 1, run: "r", corpus: "v1", location: "local", startedAt: 1, updatedAt: 2, expected: 2, observations: [] };
function memory() {
  const values = new Map<string, string>();
  const writes: string[] = [];
  return { values, writes, get: async (key: string) => values.get(key) ?? null, put: async (key: string, value: string) => { writes.push(key); values.set(key, value); } };
}

test("import is idempotent, preserves old runs, and selects the requested immutable snapshot", async () => {
  const store = memory();
  await publishReports(store, [report]);
  expect((await readSnapshot(store, null)).report).toEqual(report);
  expect(store.writes.at(-1)).toBe(INDEX_KEY);
  const count = store.writes.length;
  await publishReports(store, [report]);
  expect(store.writes.length).toBe(count);
  await publishReports(store, [{ ...report, run: "new", startedAt: 3 }]);
  expect((await readSnapshot(store, null)).report?.run).toBe("new");
  expect((await readSnapshot(store, "r")).report?.run).toBe("r");
  expect((await readSnapshot(store, "missing")).report).toBeNull();
  expect(publishReports(store, [{ ...report, location: "changed" }])).rejects.toThrow("immutable");
});

test("snapshot failure leaves the published index untouched; corrupted reads fail", async () => {
  const store = memory();
  await publishReports(store, [report]);
  const index = store.values.get(INDEX_KEY);
  await expect(publishReports({ ...store, put: async () => { throw new Error("offline"); } }, [{ ...report, run: "new" }])).rejects.toThrow("offline");
  expect(store.values.get(INDEX_KEY)).toBe(index);
  const key = store.writes[0];
  store.values.set(key, "{}");
  await expect(readSnapshot(store, "r")).rejects.toThrow("checksum");
});

test("import validation rejects duplicate observations and mismatched run provenance", () => {
  const o = { id: "a", run: "different", corpus: "v1", provider: "typeahead", q: "a", category: "prefix", round: 0, startedAt: 1, finishedAt: 2, ms: 1, outcome: "ok", status: 200, actors: [], headers: {}, error: null, cooldownUntil: 0 };
  expect(reportSchema.safeParse({ ...report, observations: [o] }).success).toBe(false);
  expect(reportSchema.safeParse({ ...report, observations: [{ ...o, run: "r" }, { ...o, run: "r" }] }).success).toBe(false);
});

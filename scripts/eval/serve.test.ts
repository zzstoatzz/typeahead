import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { directoryStore } from "./publish";
import { publishReports, readSnapshot } from "../../src/eval/archive";
import type { Report } from "../../src/eval/model";

test("preview follows atomically replaced indexes without retaining stale file sizes", async () => {
  const root = await mkdtemp(join(tmpdir(), "typeahead-preview-"));
  const store = directoryStore(root);
  const report: Report = { version: 1, run: "r", corpus: "v1", location: "local", startedAt: 1, updatedAt: 1, expected: 80, observations: [] };
  try {
    expect((await readSnapshot(store, null)).report).toBeNull();
    await publishReports(store, [report]);
    expect((await readSnapshot(store, null)).report?.location).toBe("local");
    await publishReports(store, [{ ...report, run: "new", startedAt: 2, location: "a longer location after replacement" }]);
    expect((await readSnapshot(store, null)).report?.location).toBe("a longer location after replacement");
  } finally { await rm(root, { recursive: true }); }
});

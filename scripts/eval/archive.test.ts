import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { archiveReport, readRuns } from "./archive";
import { directoryStore } from "./publish";
import { publishReports, readSnapshot } from "../../src/eval/archive";
import { nextRunAt } from "./collect";
import type { Report } from "../../src/eval/model";

const report: Report = { version: 1, run: "2026-11-16T07:25:35.153Z", corpus: "v1", location: "local", startedAt: 100, updatedAt: 200, expected: 2, observations: [] };
test("archive survives import twice and imports into a clean production-shaped store", async () => {
  const root = await mkdtemp(join(tmpdir(), "typeahead-history-"));
  try {
    await archiveReport(report, root);
    await archiveReport(report, root);
    const reports = await readRuns(root, join(root, "no-seeds"));
    expect(reports).toEqual([report]);
    const store = directoryStore(join(root, "published"));
    await publishReports(store, reports);
    expect((await readSnapshot(store, null)).report).toEqual(report);
    await expect(archiveReport({ ...report, location: "different" }, root)).rejects.toThrow("conflict");
  } finally { await rm(root, { recursive: true }); }
});

test("daily eligibility follows the last actual start; it doesn't replay missed days", () => {
  expect(nextRunAt(null)).toBe(0);
  expect(nextRunAt(100)).toBe(86_400_100);
});

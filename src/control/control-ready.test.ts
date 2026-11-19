import { afterAll, expect, test } from "bun:test";
import { controlStore } from "./control-d1-fixture";
import { META_QUERY, metaRow, parseWranglerRows, schemaProblem } from "./control-ready";

const { control, dispose } = await controlStore();
afterAll(dispose);

test("wrangler's JSON output is read as one statement's rows", () => {
  const output = JSON.stringify([{ results: [{ name: "policy_revision", value: 5 }], success: true, meta: { duration: 0 } }]);
  expect(parseWranglerRows(output, metaRow)).toEqual([{ name: "policy_revision", value: 5 }]);
  expect(() => parseWranglerRows("[]", metaRow)).toThrow();
  expect(() => parseWranglerRows(JSON.stringify([{ results: [{ name: 5 }] }]), metaRow)).toThrow();
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

import { expect, test } from "bun:test";
import { schemaProblem } from "./control-ready";

const rows = (values: unknown[][]) => JSON.stringify([{
  success: true,
  results: { columns: ["name", "value"], rows: values },
}]);

test("cf raw query confirms both control metadata counters", () => {
  expect(schemaProblem(rows([["generation", 1], ["policy_revision", 3]]))).toBeNull();
  expect(schemaProblem(rows([["policy_revision", 3]]))).toContain("no generation");
  expect(schemaProblem(rows([["generation", 1], ["policy_revision", 0]]))).toContain("no policy_revision");
});

test("a failed query or another response shape cannot admit a Worker deploy", () => {
  for (const output of [
    "", "cf d1 raw exited 1: no such table: control_meta", rows([["generation", "1"]]),
    '[{"success": false, "results": {"columns": ["name", "value"], "rows": [["generation", 1], ["policy_revision", 1]]}}]',
    '[{"success": true, "results": {"columns": ["value", "name"], "rows": []}}]',
  ]) {
    expect(schemaProblem(output)).toContain("the control database did not answer");
  }
});

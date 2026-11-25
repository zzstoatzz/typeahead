import { z } from "zod";

export const META_QUERY = "SELECT name, value FROM control_meta ORDER BY name";

const metaStatement = z.tuple([z.object({
  success: z.literal(true),
  results: z.object({
    columns: z.tuple([z.literal("name"), z.literal("value")]),
    rows: z.array(z.tuple([z.string(), z.number().int()])),
  }),
})]);

export function schemaProblem(output: string): string | null {
  let rows: [string, number][];
  try {
    rows = metaStatement.parse(JSON.parse(output))[0].results.rows;
  } catch {
    return `the control database did not answer "${META_QUERY}"; apply migrations/control-d1 (${output.trim().slice(0, 200)})`;
  }
  for (const name of ["generation", "policy_revision"]) {
    if (!rows.some(([key, value]) => key === name && value >= 1)) return `control_meta has no ${name}; apply migrations/control-d1`;
  }
  return null;
}

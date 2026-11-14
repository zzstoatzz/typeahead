import { z } from "zod";

/** Whether the control database is ready for a Worker that needs it.
 *  `scripts/control-ready.ts` is the command; `just deploy-worker` runs it
 *  first. */

/** What `wrangler d1 execute --json` prints for one statement. */
const wranglerRows = z.array(z.object({ results: z.array(z.unknown()) })).length(1);

export function parseWranglerRows<T>(output: string, row: z.ZodType<T>): T[] {
  const [statement] = wranglerRows.parse(JSON.parse(output));
  return z.array(row).parse(statement!.results);
}

/** The control database's own description of itself, as one statement's rows. */
export const META_QUERY = "SELECT name, value FROM control_meta ORDER BY name";
export const metaRow = z.object({ name: z.string(), value: z.number().int() });

/** Why the database is not ready for a Worker that needs it, or null. `output`
 *  is what `wrangler d1 execute --json` printed for `META_QUERY`. A database
 *  without the schema fails that statement, so the caller passes the error's
 *  text instead and gets it back as the reason. */
export function schemaProblem(output: string): string | null {
  let rows: z.infer<typeof metaRow>[];
  try {
    rows = parseWranglerRows(output, metaRow);
  } catch {
    return `the control database did not answer "${META_QUERY}"; apply migrations/control-d1 (${output.trim().slice(0, 200)})`;
  }
  for (const name of ["generation", "policy_revision"]) {
    if (!rows.some((row) => row.name === name && row.value >= 1)) return `control_meta has no ${name}; apply migrations/control-d1`;
  }
  return null;
}

// Carry Turso's moderation policy into the typeahead-control D1 database, and
// compare the two. Reads only; it prints SQL and never applies it.
//
//   bun scripts/control-backfill.ts sql > backfill.sql
//   bunx wrangler d1 execute typeahead-control --remote --file backfill.sql
//   bun scripts/control-backfill.ts check --remote
//
// Turso comes from TURSO_URL and TURSO_AUTH_TOKEN. `check` reads D1 through
// wrangler: pass --remote for production, or --local (with wrangler's
// --persist-to <dir> if used) for a local database. docs/slot-control.md.

import { createClient } from "@libsql/client";
import { z } from "zod";
import {
  CONTROL_QUERIES, TURSO_QUERIES, backfillSql, domainRow, overrideRow, parseWranglerRows, policyDifferences,
  type Policy,
} from "../src/control-backfill";

const DATABASE = "typeahead-control";

async function tursoPolicy(): Promise<Policy> {
  const url = process.env.TURSO_URL;
  if (!url) throw new Error("TURSO_URL is not set");
  const client = createClient({ url, authToken: process.env.TURSO_AUTH_TOKEN });
  try {
    const [overrides, domains] = await client.batch([TURSO_QUERIES.overrides, TURSO_QUERIES.domains], "read");
    return {
      overrides: z.array(overrideRow).parse(overrides!.rows.map((row) => ({ ...row }))),
      domains: z.array(domainRow).parse(domains!.rows.map((row) => ({ ...row }))),
    };
  } finally {
    client.close();
  }
}

async function wrangler(target: string[], command: string): Promise<string> {
  const child = Bun.spawn(["bunx", "wrangler", "d1", "execute", DATABASE, ...target, "--json", "--command", command], {
    stdout: "pipe", stderr: "pipe",
  });
  const [stdout, stderr, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  if (status !== 0) throw new Error(`wrangler d1 execute exited ${status}: ${stderr.trim() || stdout.trim()}`);
  return stdout;
}

async function controlPolicy(target: string[]): Promise<Policy> {
  return {
    overrides: parseWranglerRows(await wrangler(target, CONTROL_QUERIES.overrides), overrideRow),
    domains: parseWranglerRows(await wrangler(target, CONTROL_QUERIES.domains), domainRow),
  };
}

const [mode, ...target] = process.argv.slice(2);
if (mode === "sql" && target.length === 0) {
  const policy = await tursoPolicy();
  process.stdout.write(backfillSql(policy));
  console.error(`${policy.overrides.length} overrides and ${policy.domains.length} blocked domains read from Turso`);
} else if (mode === "check" && (target[0] === "--remote" || target[0] === "--local")) {
  const [turso, control] = await Promise.all([tursoPolicy(), controlPolicy(target)]);
  const differences = policyDifferences(turso, control);
  for (const line of differences) console.log(line);
  console.error(`Turso: ${turso.overrides.length} overrides, ${turso.domains.length} blocked domains; control: ${control.overrides.length}, ${control.domains.length}; ${differences.length} differences`);
  if (differences.length > 0) process.exit(1);
} else {
  console.error("usage: bun scripts/control-backfill.ts sql | check (--remote | --local [--persist-to <dir>])");
  process.exit(2);
}

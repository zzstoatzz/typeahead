// Carry Turso's moderation policy into the typeahead-control D1 database, and
// compare the two. Reads only; it prints SQL and never applies it.
//
//   bun scripts/control-backfill.ts sql > backfill.sql
//   bunx wrangler d1 execute typeahead-control --remote --file backfill.sql
//   bun scripts/control-backfill.ts check --remote
//   bun scripts/control-backfill.ts gate --remote     # schema present, then check
//   bun scripts/control-backfill.ts served https://typeahead.waow.tech
//
// `gate` is what `just deploy-worker` runs first: it refuses a control
// database without its schema, or one whose rules differ from Turso's.
// `served` compares the policy the deployed Worker serves to slots (it needs
// SLOT_CONTROL_SECRET) with Turso's, and is the gate before a slot is
// deployed with the control loop on.
//
// Turso comes from TURSO_URL and TURSO_AUTH_TOKEN. `check` reads D1 through
// wrangler: pass --remote for production, or --local (with wrangler's
// --persist-to <dir> if used) for a local database. docs/slot-control.md.

import { createClient } from "@libsql/client";
import { z } from "zod";
import {
  CONTROL_QUERIES, META_QUERY, TURSO_QUERIES, backfillSql, domainRow, overrideRow, parseWranglerRows, policyDifferences,
  schemaProblem, servedDifferences, servedPolicy,
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

async function check(target: string[]): Promise<number> {
  const [turso, control] = await Promise.all([tursoPolicy(), controlPolicy(target)]);
  const differences = policyDifferences(turso, control);
  for (const line of differences) console.log(line);
  console.error(`Turso: ${turso.overrides.length} overrides, ${turso.domains.length} blocked domains; control: ${control.overrides.length}, ${control.domains.length}; ${differences.length} differences`);
  return differences.length;
}

const [mode, ...target] = process.argv.slice(2);
if (mode === "sql" && target.length === 0) {
  const policy = await tursoPolicy();
  process.stdout.write(backfillSql(policy));
  console.error(`${policy.overrides.length} overrides and ${policy.domains.length} blocked domains read from Turso`);
} else if (mode === "check" && (target[0] === "--remote" || target[0] === "--local")) {
  if (await check(target) > 0) process.exit(1);
} else if (mode === "gate" && (target[0] === "--remote" || target[0] === "--local")) {
  const problem = schemaProblem(await wrangler(target, META_QUERY).catch((error: Error) => error.message));
  if (problem !== null) {
    console.error(`not ready: ${problem}`);
    process.exit(1);
  }
  if (await check(target) > 0) {
    console.error("not ready: the control database's rules differ from Turso's; a Worker deployed now would serve slots a policy that drops or adds rules. Run the backfill (docs/slot-control.md) and resolve what is listed.");
    process.exit(1);
  }
  console.error("the control database has its schema and holds Turso's rules");
} else if (mode === "served" && target.length === 1 && target[0]!.startsWith("http")) {
  const secret = process.env.SLOT_CONTROL_SECRET;
  if (!secret) throw new Error("SLOT_CONTROL_SECRET is not set");
  const response = await fetch(`${target[0]!.replace(/\/$/, "")}/slot/control/policy`, { headers: { Authorization: `Bearer ${secret}` }, redirect: "error" });
  if (!response.ok) {
    console.error(`not ready: ${target[0]}/slot/control/policy answered ${response.status}`);
    process.exit(1);
  }
  const served = servedPolicy.parse(await response.json());
  const differences = servedDifferences(await tursoPolicy(), served);
  for (const line of differences) console.log(line);
  console.error(`served: generation ${served.generation}, revision ${served.revision}, ${served.overrides.length} overrides, ${served.blocked_domains.length} blocked domains; ${differences.length} differences from Turso`);
  if (differences.length > 0) {
    console.error("not ready: a slot that pulled this policy would make its store equal to it. Do not deploy a slot with the control loop on.");
    process.exit(1);
  }
} else {
  console.error("usage: bun scripts/control-backfill.ts sql | check <target> | gate <target> | served <worker origin>\n  <target> is --remote, or --local [--persist-to <dir>]");
  process.exit(2);
}

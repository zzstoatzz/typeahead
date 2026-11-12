import { z } from "zod";
import { validBlockedDomain, validOverrideDid } from "./control-rules";

/** Carrying the moderation policy Turso holds into the control database, and
 *  checking the two against each other. `scripts/control-backfill.ts` is the
 *  command; docs/slot-control.md says when to run it. */

export const overrideRow = z.object({
  did: z.string().min(1),
  action: z.enum(["show", "hide"]),
  reason: z.string().nullable().transform((reason) => reason ?? ""),
  at: z.number().int(),
});
export const domainRow = z.object({
  domain: z.string().min(1),
  reason: z.string().nullable().transform((reason) => reason ?? ""),
  at: z.number().int(),
});

export type OverrideRow = z.infer<typeof overrideRow>;
export type DomainRow = z.infer<typeof domainRow>;
export type Policy = { overrides: OverrideRow[]; domains: DomainRow[] };

/** Turso names the time `created_at`, the control database `updated_at`. */
export const TURSO_QUERIES = {
  overrides: "SELECT did, action, reason, created_at AS at FROM mod_overrides ORDER BY did",
  domains: "SELECT domain, reason, created_at AS at FROM blocked_domains ORDER BY domain",
} as const;
export const CONTROL_QUERIES = {
  overrides: "SELECT did, action, reason, updated_at AS at FROM mod_overrides ORDER BY did",
  domains: "SELECT domain, reason, updated_at AS at FROM blocked_domains ORDER BY domain",
} as const;

/** What `wrangler d1 execute --json` prints for one statement. */
const wranglerRows = z.array(z.object({ results: z.array(z.unknown()) })).length(1);

export function parseWranglerRows<T>(output: string, row: z.ZodType<T>): T[] {
  const [statement] = wranglerRows.parse(JSON.parse(output));
  return z.array(row).parse(statement!.results);
}

const quote = (text: string) => `'${text.replaceAll("'", "''")}'`;
const BUMP = "UPDATE control_meta SET value = value + 1 WHERE name = 'policy_revision';";

/** SQL that adds every rule in `policy` the control database lacks and
 *  leaves the ones it has alone, so running it again changes no rule. The
 *  revision is raised before and after the rows: however the statements are
 *  interleaved with a slot's read, the last thing to happen is a new
 *  revision, and the slot reads the complete policy under it. A rule a slot
 *  would refuse is not carried: one such rule served would stop every slot
 *  taking any policy, so the whole backfill stops and names it. */
export function backfillSql(policy: Policy): string {
  for (const row of policy.overrides) {
    if (!validOverrideDid(row.did)) throw new Error(`Turso override ${JSON.stringify(row.did.slice(0, 80))} is not a DID a slot accepts; fix or remove the row in Turso first`);
  }
  for (const row of policy.domains) {
    if (!validBlockedDomain(row.domain.toLowerCase())) throw new Error(`Turso blocked domain ${JSON.stringify(row.domain.slice(0, 80))} is not a domain a slot accepts; fix or remove the row in Turso first`);
  }
  if (policy.overrides.length === 0 && policy.domains.length === 0) return "";
  const lines = [BUMP];
  for (const row of policy.overrides) {
    lines.push(`INSERT INTO mod_overrides (did, action, reason, updated_at) VALUES (${quote(row.did)}, ${quote(row.action)}, ${quote(row.reason)}, ${row.at}) ON CONFLICT(did) DO NOTHING;`);
  }
  for (const row of policy.domains) {
    lines.push(`INSERT INTO blocked_domains (domain, reason, updated_at) VALUES (${quote(row.domain.toLowerCase())}, ${quote(row.reason)}, ${row.at}) ON CONFLICT(domain) DO NOTHING;`);
  }
  lines.push(BUMP);
  return lines.join("\n") + "\n";
}

/** Every way the two stores disagree about a rule, one line each; empty when
 *  they hold the same overrides and blocked domains with the same reasons.
 *  Times are not compared: each store stamps its own write. */
export function policyDifferences(turso: Policy, control: Policy): string[] {
  const differences: string[] = [];
  const overrides = new Map(control.overrides.map((row) => [row.did, row]));
  for (const row of turso.overrides) {
    const held = overrides.get(row.did);
    overrides.delete(row.did);
    if (held === undefined) differences.push(`override ${row.did}: ${row.action} in Turso, absent from control`);
    else if (held.action !== row.action) differences.push(`override ${row.did}: ${row.action} in Turso, ${held.action} in control`);
    else if (held.reason !== row.reason) differences.push(`override ${row.did}: reason differs`);
  }
  for (const row of overrides.values()) differences.push(`override ${row.did}: ${row.action} in control, absent from Turso`);

  const domains = new Map(control.domains.map((row) => [row.domain.toLowerCase(), row]));
  for (const row of turso.domains) {
    const name = row.domain.toLowerCase();
    const held = domains.get(name);
    domains.delete(name);
    if (held === undefined) differences.push(`domain ${name}: blocked in Turso, absent from control`);
    else if (held.reason !== row.reason) differences.push(`domain ${name}: reason differs`);
  }
  for (const row of domains.values()) differences.push(`domain ${row.domain}: blocked in control, absent from Turso`);
  return differences;
}

/** The control database's own description of itself, as one statement's rows. */
export const META_QUERY = "SELECT name, value FROM control_meta ORDER BY name";
const metaRow = z.object({ name: z.string(), value: z.number().int() });

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

/** What `/slot/control/policy` serves. */
export const servedPolicy = z.object({
  generation: z.number().int(),
  revision: z.number().int(),
  overrides: z.array(z.object({ did: z.string(), action: z.enum(["show", "hide"]) })),
  blocked_domains: z.array(z.string()),
});

/** Every way the policy the Worker serves to slots differs from the rules
 *  Turso holds, one line each. A slot makes its store equal to what is
 *  served, so a rule missing here is one every slot would drop. */
export function servedDifferences(turso: Policy, served: z.infer<typeof servedPolicy>): string[] {
  const differences: string[] = [];
  const overrides = new Map(served.overrides.map((row) => [row.did, row.action]));
  for (const row of turso.overrides) {
    const action = overrides.get(row.did);
    overrides.delete(row.did);
    if (action === undefined) differences.push(`override ${row.did}: ${row.action} in Turso, not served`);
    else if (action !== row.action) differences.push(`override ${row.did}: ${row.action} in Turso, ${action} served`);
  }
  for (const [did, action] of overrides) differences.push(`override ${did}: ${action} served, absent from Turso`);
  const domains = new Set(served.blocked_domains);
  for (const row of turso.domains) {
    const name = row.domain.toLowerCase();
    if (!domains.delete(name)) differences.push(`domain ${name}: blocked in Turso, not served`);
  }
  for (const name of domains) differences.push(`domain ${name}: served, absent from Turso`);
  return differences;
}

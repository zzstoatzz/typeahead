import { d1Db, rowsAs, type SqlDb, type Stmt } from "./db";
import type { Env } from "./types";

/** The control record: what an operator or a user asked for that every store
 *  slot must come to hold. It lives in the `typeahead-control` D1 database and
 *  is the source of truth for moderation overrides, blocked domains and
 *  indexing requests. Slots pull it; nothing pushes to a slot. */

export type OverrideAction = "show" | "hide";

export type PolicySnapshot = {
  /** which control database this is; see migrations/control-d1/0002 */
  generation: number;
  revision: number;
  overrides: { did: string; action: OverrideAction }[];
  blocked_domains: string[];
};

export type IndexRequest = { seq: number; did: string };

/** The statement that gives the database a new generation. An operator runs
 *  it after restoring the database to an earlier time, so that every slot
 *  drops the positions it took from the later one. */
export const NEW_GENERATION_SQL =
  "UPDATE control_meta SET value = abs(random() % 9007199254740991) + 1 WHERE name = 'generation'";

/** A repeat request for one account inside this window adds no row. */
export const INDEX_REQUEST_REPEAT_SECONDS = 300;
/** Requests older than this are pruned; a slot that far behind is rebuilt. */
export const INDEX_REQUEST_RETENTION_SECONDS = 7 * 86_400;
export const INDEX_REQUEST_PAGE_MAX = 500;

export function controlDb(env: Pick<Env, "CONTROL_DB">): SqlDb {
  return d1Db(env.CONTROL_DB);
}

/** One policy change and the revision that announces it, in one transaction:
 *  a slot that holds revision N holds every change up to N. */
async function changePolicy(control: SqlDb, change: Stmt): Promise<void> {
  await control.batch([
    change,
    control.prepare("UPDATE control_meta SET value = value + 1 WHERE name = 'policy_revision'"),
  ]);
}

export function setOverride(control: SqlDb, did: string, action: OverrideAction, reason: string, now: number): Promise<void> {
  return changePolicy(control, control.prepare(
    `INSERT INTO mod_overrides (did, action, reason, updated_at) VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT(did) DO UPDATE SET action = ?2, reason = ?3, updated_at = ?4`,
  ).bind(did, action, reason, now));
}

export function clearOverride(control: SqlDb, did: string): Promise<void> {
  return changePolicy(control, control.prepare("DELETE FROM mod_overrides WHERE did = ?1").bind(did));
}

export function blockDomain(control: SqlDb, domain: string, reason: string, now: number): Promise<void> {
  return changePolicy(control, control.prepare(
    `INSERT INTO blocked_domains (domain, reason, updated_at) VALUES (?1, ?2, ?3)
     ON CONFLICT(domain) DO UPDATE SET reason = ?2, updated_at = ?3`,
  ).bind(domain, reason, now));
}

export function unblockDomain(control: SqlDb, domain: string): Promise<void> {
  return changePolicy(control, control.prepare("DELETE FROM blocked_domains WHERE domain = ?1").bind(domain));
}

/** The admin listings name the time `created_at`, as their callers read it. */
export async function listOverrides(control: SqlDb): Promise<{ did: string; action: string; reason: string; created_at: number }[]> {
  const { results } = await control.prepare(
    "SELECT did, action, reason, updated_at AS created_at FROM mod_overrides ORDER BY updated_at DESC, did",
  ).all<{ did: string; action: string; reason: string; created_at: number }>();
  return results;
}

export async function listBlockedDomains(control: SqlDb): Promise<{ domain: string; reason: string; created_at: number }[]> {
  const { results } = await control.prepare(
    "SELECT domain, reason, updated_at AS created_at FROM blocked_domains ORDER BY updated_at DESC, domain",
  ).all<{ domain: string; reason: string; created_at: number }>();
  return results;
}

const META = "SELECT name, value FROM control_meta WHERE name IN ('generation', 'policy_revision')";

/** Which database this is and the revision its policy stands at. */
export type ControlMeta = { generation: number; revision: number };

function metaOf(rows: { name: string; value: number }[]): ControlMeta {
  const generation = rows.find((row) => row.name === "generation");
  const revision = rows.find((row) => row.name === "policy_revision");
  if (generation === undefined || revision === undefined) {
    throw new Error("control database has no generation or policy revision; apply migrations/control-d1");
  }
  return { generation: generation.value, revision: revision.value };
}

export async function controlMeta(control: SqlDb): Promise<ControlMeta> {
  const { results } = await control.prepare(META).all<{ name: string; value: number }>();
  return metaOf(results);
}

export async function policyRevision(control: SqlDb): Promise<number> {
  return (await controlMeta(control)).revision;
}

/** The whole policy as of one revision. The three reads are one transaction,
 *  so the lists are exactly what that revision names. Reasons are operator
 *  notes and stay out of it. */
export async function policySnapshot(control: SqlDb): Promise<PolicySnapshot> {
  const [meta, overrides, domains] = await control.batch([
    control.prepare(META),
    control.prepare("SELECT did, action FROM mod_overrides ORDER BY did"),
    control.prepare("SELECT domain FROM blocked_domains ORDER BY domain"),
  ], "read");
  return {
    ...metaOf(rowsAs<{ name: string; value: number }>(meta!.results)),
    overrides: rowsAs<{ did: string; action: OverrideAction }>(overrides!.results).map((row) => ({ did: row.did, action: row.action })),
    blocked_domains: rowsAs<{ domain: string }>(domains!.results).map((row) => row.domain),
  };
}

/** Note that someone asked for `did` to be indexed. */
export async function recordIndexRequest(control: SqlDb, did: string, now: number): Promise<void> {
  await control.prepare(
    `INSERT INTO index_requests (did, requested_at)
     SELECT ?1, ?2 WHERE NOT EXISTS (
       SELECT 1 FROM index_requests WHERE did = ?1 AND requested_at > ?3)`,
  ).bind(did, now, now - INDEX_REQUEST_REPEAT_SECONDS).run();
}

/** Requests after `after` in sequence order, at most `limit`, and whether
 *  more remain. */
export async function indexRequestsAfter(control: SqlDb, after: number, limit: number): Promise<{ requests: IndexRequest[]; more: boolean }> {
  const { results } = await control.prepare(
    "SELECT seq, did FROM index_requests WHERE seq > ?1 ORDER BY seq LIMIT ?2",
  ).bind(after, limit + 1).all<IndexRequest>();
  return { requests: results.slice(0, limit).map((row) => ({ seq: row.seq, did: row.did })), more: results.length > limit };
}

/** A page of the request log for a slot whose position was taken from the
 *  database of generation `held` (null when it holds none). A position from
 *  another generation means nothing here, so that slot is served from the
 *  start of the log: nothing the log still holds is skipped, and what the
 *  slot has seen before it queues again, which changes nothing it has not
 *  already done. Starting such a slot from the newest sequence instead would
 *  lose every request made between the restore and its next poll. */
export async function indexRequestPage(
  control: SqlDb, held: number | null, after: number, limit: number,
): Promise<{ generation: number; requests: IndexRequest[]; more: boolean }> {
  const [meta, page] = await control.batch([
    control.prepare(META),
    control.prepare(
      `SELECT seq, did FROM index_requests
       WHERE seq > CASE WHEN (SELECT value FROM control_meta WHERE name = 'generation') = ?3 THEN ?1 ELSE 0 END
       ORDER BY seq LIMIT ?2`,
    ).bind(after, limit + 1, held),
  ], "read");
  const rows = rowsAs<IndexRequest>(page!.results);
  return {
    generation: metaOf(rowsAs<{ name: string; value: number }>(meta!.results)).generation,
    requests: rows.slice(0, limit).map((row) => ({ seq: row.seq, did: row.did })),
    more: rows.length > limit,
  };
}

export async function pruneIndexRequests(control: SqlDb, now: number): Promise<number> {
  const result = await control.prepare("DELETE FROM index_requests WHERE requested_at < ?1")
    .bind(now - INDEX_REQUEST_RETENTION_SECONDS).run();
  return result.meta.changes;
}

/** one DID's override, or null when it has none */
export async function getOverride(db: SqlDb, did: string): Promise<"show" | "hide" | null> {
  const row = await db.prepare("SELECT action FROM mod_overrides WHERE did = ?1").bind(did).first<{ action: "show" | "hide" }>();
  return row ? row.action : null;
}

/** every blocked domain, lowercased, as the set isDomainBlocked takes */
export async function getBlockedDomains(db: SqlDb): Promise<Set<string>> {
  const { results } = await db.prepare("SELECT domain FROM blocked_domains").all<{ domain: string }>();
  return new Set((results || []).map((row) => row.domain.toLowerCase()));
}

import { z } from "zod";
import { admittedHandle } from "./identity-policy";
import type { Stmt, TursoDB } from "./db";
import { rowsAs } from "./db";
import type { Env } from "./types";
import { authorized, parseBody, rejectUnauthorized } from "./admin-auth";
import { json } from "./utils";

export const IDENTITY_REASONS = ["identity", "account", "discovered", "plc"] as const;
export type IdentityReason = (typeof IDENTITY_REASONS)[number];

export const LEASE_SECONDS = 300;
export const MAX_LEASE = 200;
export const MAX_ATTEMPTS = 12;
export const MIN_RETRY_SECONDS = 30;
export const MAX_RETRY_SECONDS = 86_400;
const STATEMENTS_PER_TRANSACTION = 240;

/** A signal coalesces into the DID's one row. A newer signal bumps `gen`, so an
 *  in-flight completion for the older generation cannot delete the row, and
 *  pulls `due_at` forward even past an active lease. */
export function enqueueIdentity(db: TursoDB, did: string, reason: IdentityReason): Stmt {
  return db.prepare(
    `INSERT INTO identity_queue (did, gen, due_at, attempts, reason, enqueued_at, last_error)
     VALUES (?1, 1, unixepoch(), 0, ?2, unixepoch(), '')
     ON CONFLICT(did) DO UPDATE SET
       gen = identity_queue.gen + 1,
       due_at = MIN(identity_queue.due_at, unixepoch()),
       attempts = 0,
       reason = excluded.reason`,
  ).bind(did, reason);
}

/** A PLC directory operation re-requests resolution only for accounts we
 *  already index; an account we have not discovered yet gets its identity
 *  when discovery enqueues it. */
export function enqueueKnown(db: TursoDB, did: string): Stmt {
  return db.prepare(
    `INSERT INTO identity_queue (did, gen, due_at, attempts, reason, enqueued_at, last_error)
     SELECT ?1, 1, unixepoch(), 0, 'plc', unixepoch(), '' WHERE EXISTS (SELECT 1 FROM actors WHERE did = ?1)
     ON CONFLICT(did) DO UPDATE SET
       gen = identity_queue.gen + 1,
       due_at = MIN(identity_queue.due_at, unixepoch()),
       attempts = 0,
       reason = excluded.reason`,
  ).bind(did);
}

/** Must run immediately after the actor's `INSERT OR IGNORE` probe in the same
 *  transaction: `changes()` is 1 only when that probe created the row. An
 *  existing pending row is left alone, so discovery never resets a backoff. */
export function enqueueDiscovered(db: TursoDB, did: string): Stmt {
  return db.prepare(
    `INSERT INTO identity_queue (did, gen, due_at, attempts, reason, enqueued_at, last_error)
     SELECT ?1, 1, unixepoch(), 0, 'discovered', unixepoch(), '' WHERE changes() = 1
     ON CONFLICT(did) DO NOTHING`,
  ).bind(did);
}

/** Leasing pushes `due_at` out by the lease, so a resolver that dies mid-batch
 *  simply sees the work again once the lease lapses. */
export const LEASE_SQL = `UPDATE identity_queue
  SET due_at = unixepoch() + ?2, attempts = attempts + 1
  WHERE did IN (
    SELECT did FROM identity_queue WHERE due_at <= unixepoch() ORDER BY due_at LIMIT ?1
  )
  RETURNING did, gen, attempts, reason`;

const leaseRequestSchema = z.object({ limit: z.number().int().min(1).max(MAX_LEASE) });

const resolvedSchema = z.object({
  outcome: z.literal("resolved"),
  did: z.string().regex(/^did:(plc|web):/),
  gen: z.number().int().min(1),
  observed_at: z.number().int().min(1),
  handle: z.string().max(253).optional(),
  handle_verified: z.boolean().optional(),
  pds: z.string().max(512).optional(),
});

const retrySchema = z.object({
  outcome: z.literal("retry"),
  did: z.string().regex(/^did:(plc|web):/),
  gen: z.number().int().min(1),
  attempts: z.number().int().min(1),
  observed_at: z.number().int().min(1),
  delay_seconds: z.number().int(),
  pds: z.string().max(512).optional(),
  error: z.string().max(200),
});

export const identityResultSchema = z.discriminatedUnion("outcome", [resolvedSchema, retrySchema]);
export type IdentityResult = z.infer<typeof identityResultSchema>;
const completeRequestSchema = z.object({ results: z.array(identityResultSchema).min(1).max(MAX_LEASE) });

export function retryDelay(requested: number): number {
  return Math.min(MAX_RETRY_SECONDS, Math.max(MIN_RETRY_SECONDS, requested));
}

/** Identity fields are written only by an observation at least as new as the
 *  one already applied. An empty value never clears a stored one: a document
 *  without a PDS or handle is not proof the stored value is wrong. */
function applyIdentity(db: TursoDB, did: string, observedAt: number, handle: string, pds: string): Stmt {
  return db.prepare(
    `UPDATE actors SET
       handle = CASE WHEN ?3 != '' THEN ?3 ELSE handle END,
       pds = CASE WHEN ?4 != '' THEN ?4 ELSE pds END,
       identity_resolved_at = ?2,
       updated_at = unixepoch(),
       profile_checked_at = 0
     WHERE did = ?1 AND identity_resolved_at <= ?2`,
  ).bind(did, observedAt, handle, pds);
}

function completeQueued(db: TursoDB, did: string, gen: number): Stmt {
  return db.prepare("DELETE FROM identity_queue WHERE did = ?1 AND gen = ?2").bind(did, gen);
}

export type Settlement = "resolved" | "retried" | "abandoned";
export type Settled = { settlement: Settlement; stmts: Stmt[] };

/** The statements that settle one result, kept together so each DID's actor
 *  write and queue transition commit in the same transaction. */
export function settle(db: TursoDB, result: IdentityResult): Settled {
  switch (result.outcome) {
    case "resolved": {
      const handle = admittedHandle(result);
      return {
        settlement: "resolved",
        stmts: [applyIdentity(db, result.did, result.observed_at, handle, result.pds ?? ""), completeQueued(db, result.did, result.gen)],
      };
    }
    case "retry": {
      const stmts = result.pds ? [applyIdentity(db, result.did, result.observed_at, "", result.pds)] : [];
      if (result.attempts >= MAX_ATTEMPTS) {
        return { settlement: "abandoned", stmts: [...stmts, completeQueued(db, result.did, result.gen)] };
      }
      stmts.push(db.prepare(
        "UPDATE identity_queue SET due_at = unixepoch() + ?3, last_error = ?4 WHERE did = ?1 AND gen = ?2",
      ).bind(result.did, result.gen, retryDelay(result.delay_seconds), result.error));
      return { settlement: "retried", stmts };
    }
  }
}

type LeasedRow = { did: string; gen: number; attempts: number; reason: string };

export async function handleIdentityLease(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/identity/lease");
  const body = await parseBody(request, leaseRequestSchema);
  if (!body) return json({ error: "limit must be an integer from 1 to " + MAX_LEASE }, 400);
  const [result] = await db.batch([db.prepare(LEASE_SQL).bind(body.limit, LEASE_SECONDS)]);
  const jobs = rowsAs<LeasedRow>(result?.results ?? [])
    .map(({ did, gen, attempts, reason }) => ({ did, gen: Number(gen), attempts: Number(attempts), reason }));
  if (jobs.length > 0) {
    console.log(JSON.stringify({ event: "identity_lease", leased: jobs.length, requested: body.limit }));
  }
  return json({ jobs, lease_seconds: LEASE_SECONDS });
}

export async function handleIdentityComplete(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/identity/complete");
  const body = await parseBody(request, completeRequestSchema);
  if (!body) return json({ error: "invalid identity results" }, 400);

  const counts = { resolved: 0, retried: 0, abandoned: 0 } satisfies Record<Settlement, number>;
  const transactions: Stmt[][] = [[]];
  for (const result of body.results) {
    const { settlement, stmts } = settle(db, result);
    counts[settlement]++;
    if (settlement === "abandoned") {
      console.log(JSON.stringify({ event: "identity_abandoned", did: result.did, attempts: MAX_ATTEMPTS }));
    }
    let current = transactions[transactions.length - 1]!;
    if (current.length + stmts.length > STATEMENTS_PER_TRANSACTION) {
      current = [];
      transactions.push(current);
    }
    current.push(...stmts);
  }

  try {
    for (const stmts of transactions) {
      if (stmts.length > 0) await db.batch(stmts);
    }
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e);
    console.log(JSON.stringify({ event: "identity_complete_error", error, count: body.results.length }));
    return json({ error }, 500);
  }
  console.log(JSON.stringify({ event: "identity_complete", ...counts }));
  return json({ ok: true, ...counts });
}

export const PLC_CURSOR_KEY = "plc_export_cursor";
export const MAX_PLC_DIDS = 1000;
const PLC_BOOTSTRAP_MS = 5 * 60 * 1000;
const plcTimestamp = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/);
const plcRequestSchema = z.object({
  dids: z.array(z.string().regex(/^did:plc:[a-z2-7]{24}$/)).max(MAX_PLC_DIDS),
  cursor: plcTimestamp,
});

export async function handlePlcCursor(request: Request, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/identity/plc");
  const stored = await env.KV.get(PLC_CURSOR_KEY);
  // Bootstrap a few minutes back: the historical backlog belongs to offline
  // snapshot builds, not to this tail.
  return json({ cursor: stored ?? new Date(Date.now() - PLC_BOOTSTRAP_MS).toISOString(), bootstrap: stored === null });
}

/** Enqueues before it records the cursor, so a failure replays the page
 *  instead of skipping it. The cursor only moves forward. */
export async function handlePlcOperations(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/identity/plc");
  const body = await parseBody(request, plcRequestSchema);
  if (!body) return json({ error: "invalid plc operations" }, 400);

  const dids = [...new Set(body.dids)];
  let enqueued = 0;
  try {
    for (let i = 0; i < dids.length; i += STATEMENTS_PER_TRANSACTION) {
      const results = await db.batch(dids.slice(i, i + STATEMENTS_PER_TRANSACTION).map((did) => enqueueKnown(db, did)));
      for (const r of results) enqueued += r.meta.changes;
    }
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e);
    console.log(JSON.stringify({ event: "plc_enqueue_error", error, count: dids.length }));
    return json({ error }, 500);
  }

  const stored = await env.KV.get(PLC_CURSOR_KEY);
  const cursor = stored !== null && stored > body.cursor ? stored : body.cursor;
  if (cursor !== stored) await env.KV.put(PLC_CURSOR_KEY, cursor);
  console.log(JSON.stringify({ event: "plc_operations", dids: dids.length, enqueued, cursor }));
  return json({ ok: true, enqueued, cursor });
}

type QueueStatus ={ pending: number; due: number; oldest_due_at: number | null };

export async function handleIdentityStatus(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/identity/status");
  const status = await db.prepare(
    `SELECT COUNT(*) AS pending,
            COALESCE(SUM(CASE WHEN due_at <= unixepoch() THEN 1 ELSE 0 END), 0) AS due,
            MIN(due_at) AS oldest_due_at
     FROM identity_queue`,
  ).first<QueueStatus>();
  return json(status ?? { pending: 0, due: 0, oldest_due_at: null });
}

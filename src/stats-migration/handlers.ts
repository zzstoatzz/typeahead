import { authorized, parseBody, rejectUnauthorized } from "../admin-auth";
import { d1Db, type SqlDb } from "../db";
import { BackfillInputError, backfillRequestSchema, backfillStep } from "./backfill";
import { ParityInputError, parityRequestSchema, parityStep } from "./parity";
import type { Env } from "../types";
import { json } from "../utils";

/** POST /admin/stats-backfill: one step of the Turso to D1 history copy
 *  (./backfill.ts). The rows travel from Turso to D1 inside this
 *  Worker; the caller sends a position and receives counts and the next
 *  position, so driving it costs the operator's connection a few hundred
 *  small requests. `dry_run` is required: nothing is written unless the
 *  caller says false. */
export async function handleStatsBackfill(request: Request, turso: SqlDb, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/stats-backfill");
  if (!env.STATS_DB) return json({ error: "this Worker has no STATS_DB binding" }, 503);
  const body = await parseBody(request, backfillRequestSchema);
  if (!body) return json({ error: "expected { step, cursor, cut, limit, dry_run }" }, 400);
  try {
    const result = await backfillStep(turso, d1Db(env.STATS_DB), body);
    console.log(JSON.stringify({ event: "stats_backfill_step", step: result.step, cut: result.cut, dry_run: result.dry_run,
      changes: result.changes, wrote: result.wrote, next: result.next }));
    return json(result);
  } catch (error) {
    if (error instanceof BackfillInputError) return json({ error: error.message }, 400);
    throw error;
  }
}

/** POST /admin/stats-parity: one section of the Turso and D1 comparison
 *  (./parity.ts). It reads both stores and writes nothing; POST only
 *  because the request is a JSON body. */
export async function handleStatsParity(request: Request, turso: SqlDb, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/stats-parity");
  if (!env.STATS_DB) return json({ error: "this Worker has no STATS_DB binding" }, 503);
  const body = await parseBody(request, parityRequestSchema);
  if (!body) return json({ error: "expected { section, cursor, from, closed_before, limit }" }, 400);
  try {
    return json(await parityStep(turso, d1Db(env.STATS_DB), body));
  } catch (error) {
    if (error instanceof ParityInputError) return json({ error: error.message }, 400);
    throw error;
  }
}

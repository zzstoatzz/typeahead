import { d1Db, type SqlDb } from "./db";
import type { Env } from "./types";

export type StatsEnv = Pick<Env, "STATS_DB" | "STATS_DUAL_WRITE" | "STATS_READ_SOURCE">;

/** one statistics write, issued unchanged against whichever store it is handed */
export type StatsWrite = (store: SqlDb, now: number) => Promise<void>;

/** Which store answers statistics reads. `d1` needs the binding; without it
 *  the reads stay on Turso and the misconfiguration is logged. */
export function statsReadSource(env: StatsEnv): "turso" | "d1" {
  if (env.STATS_READ_SOURCE !== "d1") return "turso";
  if (env.STATS_DB) return "d1";
  console.log(JSON.stringify({ event: "stats_read_source_unavailable", wanted: "d1", using: "turso" }));
  return "turso";
}

/** The store every statistics read goes to: the cron's KV materialization,
 *  rollup and profile, the /stats tail and source list, and the /source-icon
 *  known-source check. Reads of the actor corpus (the hourly snapshot's
 *  counts, the exclusion counts) are not statistics reads and keep the Turso
 *  handle they are given. */
export function statsReadDb(env: StatsEnv, turso: SqlDb): SqlDb {
  return statsReadSource(env) === "d1" && env.STATS_DB ? d1Db(env.STATS_DB) : turso;
}

/** The D1 copy of the statistics tables, or null when this Worker has no
 *  binding or nothing asks for it. Reading from D1 implies writing to it,
 *  whatever STATS_DUAL_WRITE says: a store that is read must not go stale. */
export function statsMirror(env: StatsEnv): SqlDb | null {
  if (!env.STATS_DB) return null;
  if (env.STATS_DUAL_WRITE !== "1" && env.STATS_READ_SOURCE !== "d1") return null;
  return d1Db(env.STATS_DB);
}

async function settle(store: "turso" | "d1", write: () => Promise<void>): Promise<void> {
  try {
    await write();
  } catch (error) {
    console.log(JSON.stringify({ event: "stats_write_failed", store, error: error instanceof Error ? error.message : String(error) }));
  }
}

/** Record one statistic after the response: each store gets its own
 *  `waitUntil` and its own swallowed failure, so neither can delay or fail the
 *  response or the other store. Both writes share one clock reading. */
export function recordStats(
  ctx: Pick<ExecutionContext, "waitUntil">,
  env: StatsEnv,
  db: SqlDb | Promise<SqlDb>,
  write: StatsWrite,
): void {
  const now = Date.now();
  ctx.waitUntil(settle("turso", async () => write(await db, now)));
  const mirror = statsMirror(env);
  if (mirror) ctx.waitUntil(settle("d1", () => write(mirror, now)));
}

/** The same for a caller that has no request to outlive (the cron): the Turso
 *  failure still reaches the caller, the D1 one never does. */
export async function writeStats(env: StatsEnv, db: SqlDb, write: StatsWrite): Promise<void> {
  const now = Date.now();
  const mirror = statsMirror(env);
  const mirrored = mirror ? settle("d1", () => write(mirror, now)) : Promise.resolve();
  try {
    await write(db, now);
  } finally {
    await mirrored;
  }
}

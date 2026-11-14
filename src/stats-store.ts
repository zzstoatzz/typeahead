import { d1Db, type SqlDb } from "./db";
import type { Env } from "./types";

export type StatsEnv = Pick<Env, "STATS_DB" | "STATS_MODE">;

/** one statistics write, issued unchanged against whichever store it is handed */
export type StatsWrite = (store: SqlDb, now: number) => Promise<void>;

/** Where statistics are written and read, from the one setting STATS_MODE:
 *
 *    turso-only   Turso is written and read; D1 is not touched
 *    read-turso   both are written, Turso is read
 *    read-d1      both are written, D1 is read
 *    d1-only      D1 is written and read; Turso is not touched
 *
 *  A store that is read is always written, so there is no setting that
 *  serves a stale one. A missing or unknown value is turso-only. */
export type StatsMode = "turso-only" | "read-turso" | "read-d1" | "d1-only";

export function statsMode(env: StatsEnv): StatsMode {
  const mode = env.STATS_MODE;
  return mode === "read-turso" || mode === "read-d1" || mode === "d1-only" ? mode : "turso-only";
}

const readsD1 = (mode: StatsMode) => mode === "read-d1" || mode === "d1-only";

/** The store every statistics read goes to: the cron's KV materialization,
 *  rollup and profile, the /stats tail and source list, and the /source-icon
 *  known-source check. Reads of the actor corpus (the hourly snapshot's
 *  counts, the exclusion counts) are not statistics reads and keep the Turso
 *  handle they are given.
 *
 *  A mode that reads D1 throws without the binding: a page of Turso's numbers
 *  under a setting that says D1 would look right and be wrong, and a failed
 *  /stats stops the deploy at its smoke test. */
export function statsReadDb(env: StatsEnv, turso: SqlDb): SqlDb {
  const mode = statsMode(env);
  if (!readsD1(mode)) return turso;
  return requireD1(env, mode);
}

function requireD1(env: StatsEnv, mode: StatsMode): SqlDb {
  if (!env.STATS_DB) throw new Error(`STATS_MODE is ${mode} but this Worker has no STATS_DB binding`);
  return d1Db(env.STATS_DB);
}

/** the D1 copy of the statistics tables, when this mode writes it */
export function statsMirror(env: StatsEnv): SqlDb | null {
  return statsMode(env) !== "turso-only" && env.STATS_DB ? d1Db(env.STATS_DB) : null;
}

async function settle(store: "turso" | "d1", write: () => Promise<void>): Promise<void> {
  try {
    await write();
  } catch (error) {
    console.log(JSON.stringify({ event: "stats_write_failed", store, error: error instanceof Error ? error.message : String(error) }));
  }
}

/** Record one statistic after the response: each store written gets its own
 *  `waitUntil` and its own swallowed failure, so neither can delay or fail the
 *  response or the other store. Both writes share one clock reading. */
export function recordStats(
  ctx: Pick<ExecutionContext, "waitUntil">,
  env: StatsEnv,
  db: SqlDb | Promise<SqlDb>,
  write: StatsWrite,
): void {
  const now = Date.now();
  const mode = statsMode(env);
  if (mode === "d1-only") {
    ctx.waitUntil(settle("d1", () => write(requireD1(env, mode), now)));
    return;
  }
  ctx.waitUntil(settle("turso", async () => write(await db, now)));
  const mirror = statsMirror(env);
  if (mirror) ctx.waitUntil(settle("d1", () => write(mirror, now)));
}

/** The same for a caller that has no request to outlive (the cron). A failure
 *  of the only store written, or of Turso while both are, reaches the caller;
 *  a failure of the D1 copy never does. */
export async function writeStats(env: StatsEnv, db: SqlDb, write: StatsWrite): Promise<void> {
  const now = Date.now();
  const mode = statsMode(env);
  if (mode === "d1-only") return write(requireD1(env, mode), now);
  const mirror = statsMirror(env);
  const mirrored = mirror ? settle("d1", () => write(mirror, now)) : Promise.resolve();
  try {
    await write(db, now);
  } finally {
    await mirrored;
  }
}

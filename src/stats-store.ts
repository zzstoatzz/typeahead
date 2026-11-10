import { d1Db, type SqlDb } from "./db";
import type { Env } from "./types";

export type StatsEnv = Pick<Env, "STATS_DB" | "STATS_DUAL_WRITE">;

/** one statistics write, issued unchanged against whichever store it is handed */
export type StatsWrite = (store: SqlDb, now: number) => Promise<void>;

/** The D1 copy of the statistics tables, or null when dual write is switched
 *  off or this Worker has no binding. Turso stays the source for every read. */
export function statsMirror(env: StatsEnv): SqlDb | null {
  if (env.STATS_DUAL_WRITE !== "1" || !env.STATS_DB) return null;
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

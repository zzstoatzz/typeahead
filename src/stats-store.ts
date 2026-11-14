import { d1Db, type SqlDb } from "./db";
import type { Env } from "./types";

export type StatsEnv = Pick<Env, "STATS_DB">;

/** one statistics write, handed the store and the clock reading to file it under */
export type StatsWrite = (store: SqlDb, now: number) => Promise<void>;

/** The statistics store, the `typeahead-stats` D1 database. Every statistics
 *  write goes to it and every statistics read comes from it: the cron's KV
 *  materialization, rollup and profile, the /stats tail and source list, and
 *  the /source-icon known-source check.
 *
 *  It throws without the binding: a /stats that fails stops a deploy at its
 *  smoke test, where a page of nothing would look right and be wrong. */
export function statsDb(env: StatsEnv): SqlDb {
  if (!env.STATS_DB) throw new Error("this Worker has no STATS_DB binding");
  return d1Db(env.STATS_DB);
}

/** Record one statistic after the response. The write has its own
 *  `waitUntil` and its failure is logged and swallowed, so it can neither
 *  delay nor fail the response. */
export function recordStats(ctx: Pick<ExecutionContext, "waitUntil">, env: StatsEnv, write: StatsWrite): void {
  const now = Date.now();
  ctx.waitUntil((async () => {
    try {
      await write(statsDb(env), now);
    } catch (error) {
      console.log(JSON.stringify({ event: "stats_write_failed", store: "d1", error: error instanceof Error ? error.message : String(error) }));
    }
  })());
}

/** The same for a caller that has no request to outlive (the cron): a
 *  failure reaches the caller. */
export async function writeStats(env: StatsEnv, write: StatsWrite): Promise<void> {
  await write(statsDb(env), Date.now());
}

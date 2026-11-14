import type { Env } from "./types";

/** Whether this Worker still keeps Turso's actor tables: false only when
 *  TURSO_ACTORS is "off". Off, the ingester's endpoints answer 410 and the
 *  cron's moderation refresh does not run. */
export function tursoActorsOn(env: Pick<Env, "TURSO_ACTORS">): boolean {
  return env.TURSO_ACTORS !== "off";
}

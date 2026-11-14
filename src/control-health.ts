import { controlDb, controlMeta } from "./control-store";
import type { Env } from "./types";

/** Whether the control database can be used, and the last time a write to it
 *  failed. The admin endpoints fail loudly when it cannot; `/request-indexing`
 *  does not (a visitor's request must not start failing because of it), so
 *  this is where its failures show. docs/operating.md. */

export const CONTROL_FAILURE_KEY = "control_failure";

type HealthEnv = Pick<Env, "CONTROL_DB" | "KV">;

export type ControlFailure = { at: number; what: string; error: string };

/** Note that the control database refused `what`, with the error's text.
 *  Logged, and the last one kept in KV for `/health/control`; never throws. */
export function noteControlFailure(ctx: Pick<ExecutionContext, "waitUntil">, env: HealthEnv, what: string, error: string): void {
  const failure = { at: Date.now(), what, error: error.slice(0, 300) } satisfies ControlFailure;
  console.log(JSON.stringify({ event: "control_write_failed", ...failure }));
  ctx.waitUntil(env.KV.put(CONTROL_FAILURE_KEY, JSON.stringify(failure)).catch(() => {}));
}

export type ControlHealth =
  | { ok: true; revision: number; last_failure: ControlFailure | null }
  | { ok: false; error: string; last_failure: ControlFailure | null };

function parseFailure(text: string | null): ControlFailure | null {
  if (text === null) return null;
  try {
    const value = JSON.parse(text);
    return { at: Number(value.at), what: String(value.what), error: String(value.error) };
  } catch {
    return null;
  }
}

/** Probe the database with the read every slot poll makes. */
export async function controlHealth(env: HealthEnv): Promise<ControlHealth> {
  const last_failure = parseFailure(await env.KV.get(CONTROL_FAILURE_KEY).catch(() => null));
  try {
    const meta = await controlMeta(controlDb(env));
    return { ok: true, revision: meta.revision, last_failure };
  } catch (error) {
    return { ok: false, error: (error instanceof Error ? error.message : String(error)).slice(0, 300), last_failure };
  }
}

/** `GET /health/control`: 200 while the control database answers with its
 *  schema in place, 503 otherwise. `last_failure` is the most recent write
 *  that failed, whenever that was. */
export async function handleControlHealth(env: HealthEnv): Promise<Response> {
  const health = await controlHealth(env);
  return Response.json(health, { status: health.ok ? 200 : 503, headers: { "Cache-Control": "no-store" } });
}

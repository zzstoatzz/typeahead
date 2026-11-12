import { z } from "zod";
import type { Env } from "./types";

/** Reads of the routed store slot that are not searches: stored actors and
 *  corpus counts, on the search engine's public listener. They go the way a
 *  search does, through the transport, which follows the routing record and
 *  refuses an answer that does not carry the routed slot's identity.
 *  docs/turso-retirement.md. */

const actorSchema = z.object({
  did: z.string(),
  handle: z.string(),
  display_name: z.string(),
  avatar_url: z.string(),
  labels: z.array(z.json()),
  created_at: z.string(),
  associated: z.record(z.string(), z.json()),
  pds: z.string(),
  description: z.string(),
  banner_url: z.string(),
  followers_count: z.number(),
  follows_count: z.number(),
  posts_count: z.number(),
  quality_score: z.number(),
  /** Unix seconds of the store's last change to the actor's content; 0 on a
   *  document written before the slot carried it */
  updated_at: z.number(),
  /** whether the store has ever read the actor's appview profile */
  profile_checked: z.boolean(),
  /** the moderation decision a search on the slot would make now */
  hidden: z.boolean(),
  hidden_stored: z.boolean(),
});
export type SlotActor = z.infer<typeof actorSchema>;

const count = z.number().int().nonnegative();
const corpusSchema = z.object({
  /** counted by the index from its own documents */
  index: z.object({ actors: count, searchable: count, handle_invalid: count, quarantined: count }),
  /** the store's last corpus scan; null until it has reported one to the
   *  index, which forgets it when it restarts */
  store: z.nullable(z.object({
    live: count, total: count, with_handles: count, with_avatars: count, hidden: count, scanned_at: count,
  })),
});
export type SlotCorpus = z.infer<typeof corpusSchema>;

export const SLOT_ACTORS_MAX = 25;
const SLOT_READ_TIMEOUT_MS = 3000;

/** The slot did not give a usable answer. `status` is its HTTP status, or
 *  null when there was none: no backend configured, a timeout, or an answer
 *  in another shape. */
export class SlotReadError extends Error {
  constructor(readonly status: number | null, message: string) {
    super(message);
    this.name = "SlotReadError";
  }
}

export type SlotEnv = Pick<Env, "SEARCH_BACKEND_URL" | "SEARCH_TRANSPORT">;

async function slotGet<T>(env: SlotEnv, path: string, schema: z.ZodType<T>): Promise<T> {
  if (!env.SEARCH_BACKEND_URL) throw new SlotReadError(null, "no search backend is configured");
  const url = `${env.SEARCH_BACKEND_URL}${path}`;
  const init = { signal: AbortSignal.timeout(SLOT_READ_TIMEOUT_MS) };
  let response: Response;
  try {
    response = env.SEARCH_TRANSPORT ? await env.SEARCH_TRANSPORT.fetch(url, init) : await fetch(url, init);
  } catch (error) {
    throw new SlotReadError(null, error instanceof Error ? error.name : "fetch failed");
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {});
    throw new SlotReadError(response.status, `the slot answered ${response.status}`);
  }
  const parsed = schema.safeParse(await response.json().catch(() => null));
  if (!parsed.success) throw new SlotReadError(null, "the slot's answer has another shape");
  return parsed.data;
}

/** The stored actors named by DID or handle, in the order asked for, each
 *  once. Hidden actors are included with `hidden` set; an actor the slot
 *  does not hold is left out. */
export async function slotActors(env: SlotEnv, actors: string[]): Promise<SlotActor[]> {
  if (actors.length === 0) return [];
  if (actors.length > SLOT_ACTORS_MAX) throw new SlotReadError(null, `at most ${SLOT_ACTORS_MAX} actors per read`);
  const query = actors.map((actor) => `actor=${encodeURIComponent(actor)}`).join("&");
  return (await slotGet(env, `/actors?${query}`, z.object({ actors: z.array(actorSchema) }))).actors;
}

export function slotCorpus(env: SlotEnv): Promise<SlotCorpus> {
  return slotGet(env, "/corpus", corpusSchema);
}

/** A three-way switch for one feature leaving Turso: `turso` reads Turso,
 *  `compare` serves Turso's answer and logs how the slot's differs, `slot`
 *  reads only the slot. Anything else, or nothing, is `turso`. */
export type ReadMode = "turso" | "compare" | "slot";

export function readMode(value: string | undefined): ReadMode {
  return value === "compare" || value === "slot" ? value : "turso";
}

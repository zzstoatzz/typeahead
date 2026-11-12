import type { SqlDb } from "./db";
import { countSnapshot, writeSnapshot, type Snapshot } from "./enrichment";
import { EXCLUSIONS_KEY, countExclusions, exclusionsDue, type ExclusionsKv, type SearchExclusions } from "./exclusions";
import { readMode, slotCorpus, type SlotCorpus, type SlotEnv } from "./slot-read";
import { writeStats, type StatsEnv } from "./stats-store";
import type { Env } from "./types";

/** The corpus numbers /stats shows, under CORPUS_MODE: counted in Turso's
 *  actor tables, or taken from the routed store slot, or Turso's with the
 *  slot's logged beside them. Where the numbers are then kept (the
 *  `snapshots` table, KV) is not decided here. docs/turso-retirement.md. */

/** A store's corpus scan runs hourly. One this old means the scanner or its
 *  report to the index has stopped, and an hour's snapshot is left out
 *  rather than repeated. */
export const STORE_COUNT_MAX_AGE_S = 6 * 3600;

/** The hourly snapshot row from a slot's answer, or null when the store has
 *  not reported a count, or not recently. The store counts what Turso's
 *  snapshot does: `total` is live actors less the hidden ones, the other two
 *  are live actors with a handle and with an avatar. */
export function slotSnapshot(corpus: SlotCorpus, now = Date.now()): Snapshot | null {
  const store = corpus.store;
  if (!store || now / 1000 - store.scanned_at > STORE_COUNT_MAX_AGE_S) return null;
  return {
    hour: Math.floor(now / 3_600_000),
    total: store.total,
    with_handles: store.with_handles,
    with_avatars: store.with_avatars,
    hidden: store.hidden,
  };
}

/** What search excludes, from the index's own counts. Every document has a
 *  handle, so the documents that are not searchable are the hidden ones and
 *  those whose handle failed verification; a slot recomputes `hidden` with
 *  every change, so there are no stale flags to report. */
export function slotExclusions(corpus: SlotCorpus, now: Date): SearchExclusions {
  const { actors, searchable, handle_invalid, quarantined } = corpus.index;
  return {
    version: 2,
    as_of: now.toISOString(),
    policy_hidden: Math.max(0, actors - searchable - handle_invalid),
    stale_hidden_flags: 0,
    handle_invalid,
    quarantined,
  };
}

type CorpusEnv = SlotEnv & StatsEnv & Pick<Env, "CORPUS_MODE">;

/** In compare mode the slot is asked only to be logged: its failure is one
 *  line and Turso's count is recorded as on any other hour. */
async function slotForComparison(env: SlotEnv, what: string): Promise<SlotCorpus | null> {
  try {
    return await slotCorpus(env);
  } catch (error) {
    console.log(JSON.stringify({ event: "corpus_compare_failed", what, error: error instanceof Error ? error.message : String(error) }));
    return null;
  }
}

/** Record this hour's actor counts. In slot mode a slot that does not
 *  answer fails the step, as Turso failing does; a slot whose store has no
 *  recent count leaves the hour without a row. */
export async function recordSnapshot(db: SqlDb, env: CorpusEnv, now = Date.now()): Promise<void> {
  const mode = readMode(env.CORPUS_MODE);
  let snapshot: Snapshot | null;
  if (mode === "slot") {
    snapshot = slotSnapshot(await slotCorpus(env), now);
    if (!snapshot) console.log(JSON.stringify({ event: "corpus_snapshot_skipped", reason: "the slot's store has no recent count" }));
  } else {
    snapshot = await countSnapshot(db, now);
    if (mode === "compare") {
      const corpus = await slotForComparison(env, "snapshot");
      if (corpus) console.log(JSON.stringify({ event: "corpus_compare", what: "snapshot", turso: snapshot, slot: slotSnapshot(corpus, now) }));
    }
  }
  if (snapshot) {
    const row = snapshot;
    await writeStats(env, db, (store) => writeSnapshot(store, row));
  }
}

/** Record what search excludes, once a day. */
export async function recordExclusions(
  db: SqlDb,
  kv: ExclusionsKv,
  env: SlotEnv & Pick<Env, "CORPUS_MODE">,
  now = new Date(),
): Promise<SearchExclusions | null> {
  if (!await exclusionsDue(kv, now)) return null;
  const mode = readMode(env.CORPUS_MODE);
  let exclusions: SearchExclusions;
  if (mode === "slot") {
    exclusions = slotExclusions(await slotCorpus(env), now);
  } else {
    exclusions = await countExclusions(db, now);
    if (mode === "compare") {
      const corpus = await slotForComparison(env, "exclusions");
      if (corpus) console.log(JSON.stringify({ event: "corpus_compare", what: "exclusions", turso: exclusions, slot: slotExclusions(corpus, now) }));
    }
  }
  await kv.put(EXCLUSIONS_KEY, JSON.stringify(exclusions));
  return exclusions;
}

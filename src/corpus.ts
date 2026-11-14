import type { SqlDb } from "./db";
import { writeSnapshot, type Snapshot } from "./enrichment";
import { EXCLUSIONS_KEY, exclusionsDue, type ExclusionsKv, type SearchExclusions } from "./exclusions";
import { slotCorpus, type SlotCorpus, type SlotEnv } from "./slot-read";
import { writeStats, type StatsEnv } from "./stats-store";

/** The corpus numbers /stats shows, taken from the routed store slot. Where
 *  the numbers are then kept (the `snapshots` table, KV) is not decided here. */

/** A store's corpus scan runs hourly. One this old means the scanner or its
 *  report to the index has stopped, and an hour's snapshot is left out
 *  rather than repeated. */
export const STORE_COUNT_MAX_AGE_S = 6 * 3600;

/** The hourly snapshot row from a slot's answer, or null when the store has
 *  not reported a count, or not recently. `total` is live actors less the
 *  hidden ones, the other two are live actors with a handle and with an
 *  avatar. */
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

type CorpusEnv = SlotEnv & StatsEnv;

/** Record this hour's actor counts. A slot that does not answer fails the
 *  step; a slot whose store has no recent count leaves the hour without a
 *  row. */
export async function recordSnapshot(db: SqlDb, env: CorpusEnv, now = Date.now()): Promise<void> {
  const snapshot = slotSnapshot(await slotCorpus(env), now);
  if (!snapshot) {
    console.log(JSON.stringify({ event: "corpus_snapshot_skipped", reason: "the slot's store has no recent count" }));
    return;
  }
  await writeStats(env, db, (store) => writeSnapshot(store, snapshot));
}

/** Record what search excludes, once a day. */
export async function recordExclusions(kv: ExclusionsKv, env: SlotEnv, now = new Date()): Promise<SearchExclusions | null> {
  if (!await exclusionsDue(kv, now)) return null;
  const exclusions = slotExclusions(await slotCorpus(env), now);
  await kv.put(EXCLUSIONS_KEY, JSON.stringify(exclusions));
  return exclusions;
}

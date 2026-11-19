import { writeSnapshot, type Snapshot } from "./enrichment";
import { EXCLUSIONS_KEY, exclusionsDue, type ExclusionsKv, type SearchExclusions } from "./exclusions";
import { slotCorpus, type SlotCorpus, type SlotEnv } from "../slot-read";
import { writeStats, type StatsEnv } from "./stats-store";

/** The corpus numbers /stats shows, taken from the routed store slot. Where
 *  the numbers are then kept (the `snapshots` table, KV) is not decided here. */

/** Reject stale legacy reports during the rolling upgrade. */
export const STORE_COUNT_MAX_AGE_S = 6 * 3600;

/** Committed index counters, with legacy reports accepted during upgrades. */
export function slotSnapshot(corpus: SlotCorpus, now = Date.now()): Snapshot | null {
  const store = corpus.index.statistics_version === 1 ? corpus.counts : corpus.store;
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
export function slotExclusions(corpus: SlotCorpus, now: Date): SearchExclusions | null {
  const { actors, searchable, handle_invalid, quarantined } = corpus.index;
  if (handle_invalid === null || quarantined === null) return null;
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

/** Save committed counts without asking the slot to calculate anything. */
export async function recordSnapshot(env: CorpusEnv, now = Date.now()): Promise<void> {
  const snapshot = slotSnapshot(await slotCorpus(env), now);
  if (!snapshot) {
    console.log(JSON.stringify({ event: "corpus_snapshot_skipped", reason: "the slot has no recent committed count" }));
    return;
  }
  await writeStats(env, (store) => writeSnapshot(store, snapshot));
}

/** Record what search excludes, once a day. */
export async function recordExclusions(kv: ExclusionsKv, env: SlotEnv, now = new Date()): Promise<SearchExclusions | null> {
  if (!await exclusionsDue(kv, now)) return null;
  const exclusions = slotExclusions(await slotCorpus(env), now);
  if (!exclusions) return null;
  await kv.put(EXCLUSIONS_KEY, JSON.stringify(exclusions));
  return exclusions;
}

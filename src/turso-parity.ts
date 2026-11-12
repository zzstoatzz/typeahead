import type { Snapshot } from "./enrichment";
import { compareSummary, type ProfileView } from "./handlers/profiles";

/** The comparisons `scripts/turso-parity.ts` runs before a feature's mode is
 *  moved from compare to slot: what Turso answers against what a store slot
 *  answers, for a sample of actors and for the corpus counts.
 *  docs/turso-retirement.md. */

/** The columns `profileView` reads, for `count` visible actors with a handle
 *  starting at a rowid. A run of neighbouring rowids is one draw of the
 *  sample; many short runs from random starts spread it over the table. */
export const SAMPLE_SQL = `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds,
       description, banner_url, followers_count, follows_count, posts_count, profile_checked_at, updated_at
  FROM actors WHERE rowid >= ?1 AND hidden = 0 AND handle != '' ORDER BY rowid LIMIT ?2`;
export const SAMPLE_RUN = 5;

/** `runs` rowids to start a run at, from a seeded generator so a sample can
 *  be drawn again. */
export function sampleStarts(maxRowid: number, runs: number, seed: number): number[] {
  let state = seed >>> 0 || 1;
  const next = () => {
    state ^= state << 13; state >>>= 0;
    state ^= state >>> 17;
    state ^= state << 5; state >>>= 0;
    return state / 0x1_0000_0000;
  };
  return Array.from({ length: runs }, () => 1 + Math.floor(next() * maxRowid));
}

/** How a slot's profiles differ from Turso's for one sample. It passes when
 *  the share of sampled actors that are missing from the slot, or differ in
 *  a field both stores should agree on, is at most `maxRate`. Counts and
 *  indexedAt are reported and not judged: each store reads the appview on
 *  its own schedule. */
export function profileParity(turso: ProfileView[], slot: ProfileView[], maxRate: number) {
  const { event: _event, asked: _asked, ...summary } = compareSummary(turso.length, turso, slot);
  const sampled = turso.length;
  const failing = summary.only_turso + summary.settled_differ;
  return {
    sampled,
    ...summary,
    failing_rate: sampled === 0 ? 1 : failing / sampled,
    max_rate: maxRate,
    passed: sampled > 0 && failing / sampled <= maxRate,
  };
}

const COUNTED = ["total", "with_handles", "with_avatars"] as const;

/** The hourly snapshot's numbers from each side. It passes when each of the
 *  three /stats shows is within `maxRate` of Turso's; `hidden` is reported
 *  and not judged. */
export function corpusParity(turso: Snapshot | null, slot: Snapshot | null, maxRate: number) {
  const differences = COUNTED.map((field) => {
    const expected = turso?.[field] ?? 0;
    const actual = slot?.[field] ?? 0;
    return { field, turso: expected, slot: actual, rate: expected === 0 ? 1 : Math.abs(actual - expected) / expected };
  });
  return {
    turso,
    slot,
    differences,
    max_rate: maxRate,
    passed: turso !== null && slot !== null && differences.every((difference) => difference.rate <= maxRate),
  };
}

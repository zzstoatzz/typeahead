import type { Stmt, TursoDB } from "./db";
import type { Env } from "./types";
import { BSKY_GET_PROFILES_URL } from "./types";
import { extractProfileFields, fetchProfileFromPds } from "./utils";
import { getOverrides, getBlockedDomains, isDomainBlocked } from "./moderation";
import { recordActorDelta } from "./metrics";

type CurRow = {
  handle: string; hidden: number; labels: string;
  display_name: string; avatar_url: string; created_at: string; associated: string;
  followers_count: number; follows_count: number; posts_count: number; quality_score: number;
};

type ActorRow = CurRow & {
  rowid: number; did: string; pds: string;
};

export function isProfileIdentical(
  cur: CurRow,
  f: ReturnType<typeof extractProfileFields>,
): boolean {
  return (
    cur.hidden === f.hidden &&
    cur.labels === f.labels &&
    cur.handle === f.handle &&
    cur.display_name === f.displayName &&
    cur.avatar_url === f.avatarCid &&
    cur.created_at === f.createdAt &&
    cur.associated === f.associated &&
    cur.followers_count === f.followersCount &&
    cur.follows_count === f.followsCount &&
    cur.posts_count === f.postsCount &&
    Math.round(cur.quality_score * 100) === Math.round(f.qualityScore * 100)
  );
}

/** refresh moderation labels, walking the full index over multiple cron runs */
export async function refreshModeration(db: TursoDB, env: Env): Promise<void> {
  // Two-pool selector — replaces the old rowid cursor that took ~245 days to
  // walk the corpus and treated viral handles the same as 2022-dormant ones.
  //
  //   PRIORITY POOL (800/run): ORDER BY staleness × value × activity DESC.
  //     - staleness: now - profile_checked_at (NULL → very stale)
  //     - value:     log(1 + quality_score), so popular accounts refresh more
  //     - activity:  recent firehose touch via last_activity_at boosts further
  //   COLD FLOOR    (200/run): ORDER BY profile_checked_at ASC NULLS FIRST.
  //     Guarantees the long tail gets touched without starving on score=0
  //     accounts that never get a priority bump. Also picks up actors whose
  //     firehose enqueue set profile_checked_at=0 (see handlers/ingest.ts).
  //
  // Per Turso Scaler (100B reads/mo): one full scan per tick ~ 6M reads × 24/day
  // × 30 = 4.3B/mo ≈ 4% of budget. Will add a functional index later.
  const PRIORITY_N = 800;
  const FLOOR_N = 200;

  const priorityRows = (await db.prepare(
    `SELECT rowid, did, handle, hidden, labels, pds,
            display_name, avatar_url, created_at, associated,
            followers_count, follows_count, posts_count, quality_score
     FROM actors
     ORDER BY (unixepoch() - COALESCE(profile_checked_at, 0))
              * (1.0 + ln(1.0 + COALESCE(quality_score, 0)))
              * (CASE WHEN last_activity_at > unixepoch() - 86400 THEN 3.0
                      WHEN last_activity_at > unixepoch() - 604800 THEN 1.5
                      ELSE 1.0 END)
              DESC
     LIMIT ?1`
  ).bind(PRIORITY_N).all<ActorRow>()).results ?? [];

  const floorRows = (await db.prepare(
    `SELECT rowid, did, handle, hidden, labels, pds,
            display_name, avatar_url, created_at, associated,
            followers_count, follows_count, posts_count, quality_score
     FROM actors
     ORDER BY COALESCE(profile_checked_at, 0) ASC
     LIMIT ?1`
  ).bind(FLOOR_N).all<ActorRow>()).results ?? [];

  // dedup: priority pool wins (cold floor is a backstop, not a duplicate-fetcher).
  const seen = new Set(priorityRows.map((r) => r.did));
  const results = [...priorityRows, ...floorRows.filter((r) => !seen.has(r.did))];

  if (results.length === 0) {
    console.log(JSON.stringify({ event: "moderation_refresh", status: "empty" }));
    return;
  }

  let checked = 0;
  let changed = 0;
  let deleted = 0;
  let skipped = 0;
  const t0 = Date.now();

  // fetch all overrides for this page so cron respects them
  const overrides = await getOverrides(db, results.map((r) => r.did));
  // domain blocks must be re-applied here too — else this recompute would
  // un-hide a domain-blocked account whose labels alone don't hide it.
  const blockedDomains = await getBlockedDomains(db);

  // batch into groups of 25 (getProfiles limit), ~200ms pause between calls
  for (let i = 0; i < results.length; i += 25) {
    const batch = results.slice(i, i + 25);
    const params = batch.map((r) => `actors=${encodeURIComponent(r.did)}`).join("&");
    try {
      if (i > 0) await new Promise((r) => setTimeout(r, 200));

      const res = await fetch(`${BSKY_GET_PROFILES_URL}?${params}`);
      if (res.status === 429) {
        // bail — next tick re-selects from scratch via the priority queue,
        // so rate-limited progress just shrinks this run's batch.
        console.log(JSON.stringify({ event: "moderation_refresh", status: "rate_limited", checked, changed }));
        return;
      }
      if (!res.ok) continue;

      const data: any = await res.json();
      const profiles: any[] = data.profiles || [];
      checked += profiles.length;

      // build a lookup of current DB state for this batch
      const current = new Map(batch.map((r) => [r.did, r]));

      const stmts: Stmt[] = [];
      for (const p of profiles) {
        const f = extractProfileFields(p, overrides.get(p.did) ?? null, isDomainBlocked(p.handle || "", blockedDomains));
        const cur = current.get(p.did);
        // skip ONLY if every materialized field is identical. Quality score is
        // compared rounded — extractProfileFields already rounds to 2dp, but
        // float-eq across a JSON round-trip is fragile.
        if (cur && isProfileIdentical(cur, f)) { skipped++; continue; }

        stmts.push(
          db.prepare(
            `UPDATE actors SET hidden = ?1,
               handle = COALESCE(NULLIF(?3, ''), handle),
               display_name = COALESCE(NULLIF(?4, ''), display_name),
               avatar_url = COALESCE(NULLIF(?5, ''), avatar_url),
               labels = ?6,
               created_at = COALESCE(NULLIF(?7, ''), created_at),
               associated = COALESCE(NULLIF(?8, '{}'), associated),
               followers_count = COALESCE(NULLIF(?9, 0), followers_count),
               follows_count = COALESCE(NULLIF(?10, 0), follows_count),
               posts_count = COALESCE(NULLIF(?11, 0), posts_count),
               quality_score = COALESCE(NULLIF(?12, 0), quality_score),
               updated_at = unixepoch()
             WHERE did = ?2`
          ).bind(f.hidden, p.did, f.handle, f.displayName, f.avatarCid, f.labels,
            f.createdAt, f.associated, f.followersCount, f.followsCount, f.postsCount, f.qualityScore)
        );
      }
      if (stmts.length > 0) {
        const batchResults = await db.batch(stmts);
        changed += batchResults.filter((r) => r.meta.changes > 0).length;
      }

      // PDS fallback for unreturned DIDs — check if bsky banned them
      const returnedDids = new Set(profiles.map((p: any) => p.did));
      const pdsStmts: Stmt[] = [];
      for (const r of batch) {
        if (returnedDids.has(r.did)) continue;
        const override = overrides.get(r.did) ?? null;
        if (r.pds && (override === 'show' || !override)) {
          let isTakedown = override === 'show';
          if (!isTakedown) {
            try {
              const probe = await fetch(
                `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(r.did)}`
              );
              if (!probe.ok) {
                const err: any = await probe.json().catch(() => ({}));
                isTakedown = err.error === 'AccountTakedown';
              }
            } catch {}
          }
          if (isTakedown) {
            const pdsProfile = await fetchProfileFromPds(r.did, r.pds);
            if (pdsProfile) {
              const f = extractProfileFields(pdsProfile, override, isDomainBlocked(r.handle || "", blockedDomains));
              const cur = current.get(r.did);
              if (cur && cur.hidden === f.hidden && cur.labels === f.labels) continue;
              pdsStmts.push(
                db.prepare(
                  `UPDATE actors SET hidden = ?1,
                     display_name = COALESCE(NULLIF(?3, ''), display_name),
                     avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
                     labels = ?5,
                     created_at = COALESCE(NULLIF(?6, ''), created_at),
                     associated = COALESCE(NULLIF(?7, '{}'), associated),
                     updated_at = unixepoch()
                   WHERE did = ?2`
                ).bind(f.hidden, r.did, f.displayName, f.avatarCid, f.labels, f.createdAt, f.associated)
              );
              returnedDids.add(r.did); // don't delete this actor
            }
          }
        }
      }
      if (pdsStmts.length > 0) {
        const batchResults = await db.batch(pdsStmts);
        changed += batchResults.filter((r) => r.meta.changes > 0).length;
      }

      // dead-actor cleanup: DIDs not returned by getProfiles with empty handles
      const deadDids = batch.filter((r) => !returnedDids.has(r.did) && r.handle === "");
      if (deadDids.length > 0) {
        const delStmts: Stmt[] = deadDids.flatMap((r) => [
          db.prepare("DELETE FROM actors WHERE did = ?1").bind(r.did),
          db.prepare("INSERT OR REPLACE INTO tombstones (did, deleted_at) VALUES (?1, unixepoch())").bind(r.did),
        ]);
        await db.batch(delStmts);
        deleted += deadDids.length;
        await recordActorDelta(db, { actors: -deadDids.length }).catch(() => {});
      }

      // ADVANCE THE WALK CURSOR for everything we confirmed alive this run —
      // changed OR unchanged. Both selector pools order by profile_checked_at;
      // if we never bump it, the same head is re-selected every hour and the
      // rest of the corpus starves — stale avatars and counts never self-heal.
      // (returnedDids = getProfiles hits ∪ PDS-resolved takedowns; excludes the
      // dead rows just deleted.)
      const checkedAlive = [...returnedDids];
      if (checkedAlive.length > 0) {
        const ph = checkedAlive.map((_, i) => `?${i + 1}`).join(",");
        await db.prepare(
          `UPDATE actors SET profile_checked_at = unixepoch() WHERE did IN (${ph})`
        ).bind(...checkedAlive).run();
      }
    } catch {
      // best-effort — skip failures
    }
  }

  // prune old tombstones (> 7 days)
  await db.prepare("DELETE FROM tombstones WHERE deleted_at < unixepoch() - 604800").run();

  if (deleted > 0) {
    console.log(JSON.stringify({ event: "mod_cleanup", deleted }));
  }
  console.log(JSON.stringify({
    event: "moderation_refresh",
    checked, changed, skipped, deleted,
    priority: priorityRows.length, floor: floorRows.length,
    ms: Date.now() - t0,
  }));
}

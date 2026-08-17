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

/** refresh moderation labels and profile aggregates for a bounded batch.
 *
 *  THE CRON CONTRACT (see CLAUDE.md): every selector here must be an index
 *  SEARCH, never a corpus scan, and every batch size must state what it can
 *  honestly cover. The previous two-pool selector broke both rules: it ran
 *  two full-table ORDER BY scans over ~11M rows every hour to pick 1,000, and
 *  its doc claimed it was "walking the full index over multiple cron runs" —
 *  at 24k actors/day against an 11M corpus growing 70k/day, that walk was
 *  never going to happen. What it could honestly do is what this now says
 *  it does:
 *
 *  QUALITY WALK (800/run): a keyset cursor (KV `mod_walk_cursor`) stepping
 *    down idx_actors_visible_quality_score. The scored-visible pool — every
 *    actor a search can actually surface with ranking weight — measured
 *    41,355 rows on 2026-08-16, so a full sweep completes in ~52 runs:
 *    every rankable actor's labels/avatar/counts re-checked every ~2-3 days,
 *    deterministically, with the sweep position observable in KV. The keyset
 *    values are inlined as numeric literals, NOT bound params — the OR-keyset
 *    shape degrades to a full index scan on Turso when the values arrive as
 *    hrana params (the Aug 2026 staleness incident; smoke EXPLAIN-guards it).
 *
 *  EVENT QUEUE (200/run): actors the firehose re-touched — handlers/ingest.ts
 *    resets profile_checked_at=0 on profile/identity events so appview
 *    aggregates get re-fetched. Served newest-activity-first by the partial
 *    index idx_actors_refresh_queue. The same index also holds the historical
 *    never-checked backlog (profile_checked_at=0 since insert); reading DESC
 *    means live activity always wins, and draining that backlog is explicitly
 *    NOT this job — it belongs to the typeahead-enrich-backfill Prefect
 *    deployment (my-prefect-server repo, daily on heavypad).
 *
 *  Score-0 / hidden actors outside both queues are reached on firehose
 *  activity (event queue) or by bulk runs — the cron makes no pretense of
 *  touching all 11M rows on any schedule. */
export async function refreshModeration(db: TursoDB, env: Env): Promise<void> {
  const WALK_N = 800;
  const EVENT_N = 200;
  const ACTOR_COLS = `rowid, did, handle, hidden, labels, pds,
            display_name, avatar_url, created_at, associated,
            followers_count, follows_count, posts_count, quality_score`;

  let cursor: { qs: number; rowid: number } | null = null;
  try {
    cursor = JSON.parse((await env.KV.get("mod_walk_cursor")) || "null");
  } catch {}
  const qs = Number(cursor?.qs);
  const rid = Number(cursor?.rowid);
  const keyset = Number.isFinite(qs) && Number.isFinite(rid)
    ? `AND (quality_score < ${qs} OR (quality_score = ${qs} AND rowid < ${rid}))`
    : "";
  // predicates must match idx_actors_visible_quality_score's WHERE clause
  // exactly or the partial index is unusable and this reverts to a scan.
  const walkRows = (await db.prepare(
    `SELECT ${ACTOR_COLS} FROM actors
     WHERE handle != '' AND hidden = 0 AND quality_score > 0 ${keyset}
     ORDER BY quality_score DESC, rowid DESC
     LIMIT ${WALK_N}`
  ).all<ActorRow>()).results ?? [];

  if (walkRows.length < WALK_N) {
    // pool exhausted — wrap to the top next run
    await env.KV.delete("mod_walk_cursor").catch(() => {});
  } else {
    const last = walkRows[walkRows.length - 1];
    await env.KV.put(
      "mod_walk_cursor",
      JSON.stringify({ qs: last.quality_score, rowid: last.rowid })
    ).catch(() => {});
  }

  const eventRows = (await db.prepare(
    `SELECT ${ACTOR_COLS} FROM actors
     WHERE profile_checked_at = 0 AND handle != ''
     ORDER BY updated_at DESC
     LIMIT ?1`
  ).bind(EVENT_N).all<ActorRow>()).results ?? [];

  // dedup: event queue wins — a firehose-touched actor is fresher signal than
  // its scheduled sweep slot, and the walk cursor has already moved past it.
  const seen = new Set(eventRows.map((r) => r.did));
  const results = [...eventRows, ...walkRows.filter((r) => !seen.has(r.did))];

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
      // a 200 without a profiles array is an infra fault, not "nobody exists" —
      // treating it as empty would feed every empty-handle DID in the batch to
      // the dead-actor DELETE below (the account-info lesson: distinguish
      // protocol-level absence from infrastructure failure).
      if (!Array.isArray(data?.profiles)) continue;
      const profiles: any[] = data.profiles;
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
    walk: walkRows.length, events: eventRows.length,
    walk_cursor_qs: walkRows.length === WALK_N ? walkRows[walkRows.length - 1].quality_score : null,
    ms: Date.now() - t0,
  }));
}

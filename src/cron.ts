import type { Stmt, TursoDB } from "./db";
import type { Env } from "./types";
import { BSKY_GET_PROFILES_URL } from "./types";
import { extractProfileFields } from "./utils";
import { recordActorDelta } from "./metrics";

/** refresh moderation labels, walking the full index over multiple cron runs */
export async function refreshModeration(db: TursoDB, env: Env): Promise<void> {
  // resume where we left off (rowid cursor persisted in KV)
  const cursorStr = await env.KV.get("mod_cursor");
  const cursor = cursorStr ? Number(cursorStr) : 0;

  const { results } = await db.prepare(
    "SELECT rowid, did, handle, hidden, labels FROM actors WHERE rowid > ?1 ORDER BY rowid ASC LIMIT 1000"
  ).bind(cursor).all<{ rowid: number; did: string; handle: string; hidden: number; labels: string }>();

  if (!results || results.length === 0) {
    // wrapped around — reset cursor for next run
    await env.KV.put("mod_cursor", "0");
    console.log(JSON.stringify({ event: "moderation_refresh", status: "wrapped", cursor }));
    return;
  }

  let checked = 0;
  let changed = 0;
  let deleted = 0;
  let skipped = 0;
  const t0 = Date.now();

  // batch into groups of 25 (getProfiles limit), ~200ms pause between calls
  for (let i = 0; i < results.length; i += 25) {
    const batch = results.slice(i, i + 25);
    const params = batch.map((r) => `actors=${encodeURIComponent(r.did)}`).join("&");
    try {
      if (i > 0) await new Promise((r) => setTimeout(r, 200));

      const res = await fetch(`${BSKY_GET_PROFILES_URL}?${params}`);
      if (res.status === 429) {
        // save progress and bail — pick up here next run
        const lastRowid = results[Math.max(0, i - 1)].rowid;
        await env.KV.put("mod_cursor", String(lastRowid));
        console.log(JSON.stringify({ event: "moderation_refresh", status: "rate_limited", checked, changed, cursor: lastRowid }));
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
        const f = extractProfileFields(p);
        const cur = current.get(p.did);
        // skip no-op updates — only write if labels or hidden actually changed
        if (cur && cur.hidden === f.hidden && cur.labels === f.labels) { skipped++; continue; }

        stmts.push(
          db.prepare(
            `UPDATE actors SET hidden = ?1,
               handle = COALESCE(NULLIF(?3, ''), handle),
               display_name = COALESCE(NULLIF(?4, ''), display_name),
               avatar_url = COALESCE(NULLIF(?5, ''), avatar_url),
               labels = ?6,
               created_at = COALESCE(NULLIF(?7, ''), created_at),
               associated = COALESCE(NULLIF(?8, '{}'), associated)
             WHERE did = ?2`
          ).bind(f.hidden, p.did, f.handle, f.displayName, f.avatarCid, f.labels,
            f.createdAt, f.associated)
        );
      }
      if (stmts.length > 0) {
        const batchResults = await db.batch(stmts);
        changed += batchResults.filter((r) => r.meta.changes > 0).length;
      }

      // dead-actor cleanup: DIDs not returned by getProfiles with empty handles
      const returnedDids = new Set(profiles.map((p: any) => p.did));
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
    } catch {
      // best-effort — skip failures
    }
  }

  // save cursor at end of this page
  const lastRowid = results[results.length - 1].rowid;
  await env.KV.put("mod_cursor", String(lastRowid));

  // prune old tombstones (> 7 days)
  await db.prepare("DELETE FROM tombstones WHERE deleted_at < unixepoch() - 604800").run();

  if (deleted > 0) {
    console.log(JSON.stringify({ event: "mod_cleanup", deleted }));
  }
  console.log(JSON.stringify({ event: "moderation_refresh", checked, changed, skipped, deleted, cursor: lastRowid, ms: Date.now() - t0 }));
}

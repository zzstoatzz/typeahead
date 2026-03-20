import type { Stmt, TursoDB } from "./db";
import type { Env } from "./types";
import { BSKY_GET_PROFILES_URL } from "./types";
import { shouldHide } from "./moderation";
import { extractAvatarCid } from "./utils";

/** refresh moderation labels, walking the full index over multiple cron runs */
export async function refreshModeration(db: TursoDB, env: Env): Promise<void> {
  // resume where we left off (rowid cursor persisted in KV)
  const cursorStr = await env.KV.get("mod_cursor");
  const cursor = cursorStr ? Number(cursorStr) : 0;

  const { results } = await db.prepare(
    "SELECT rowid, did FROM actors WHERE rowid > ?1 ORDER BY rowid ASC LIMIT 1000"
  ).bind(cursor).all<{ rowid: number; did: string }>();

  if (!results || results.length === 0) {
    // wrapped around — reset cursor for next run
    await env.KV.put("mod_cursor", "0");
    console.log(JSON.stringify({ event: "moderation_refresh", status: "wrapped", cursor }));
    return;
  }

  let checked = 0;
  let changed = 0;

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

      const stmts: Stmt[] = [];
      for (const p of profiles) {
        const hide = shouldHide(p.labels) ? 1 : 0;
        const avatarCid = extractAvatarCid(p.avatar || '');
        stmts.push(
          db.prepare(
            `UPDATE actors SET hidden = ?1,
               handle = COALESCE(NULLIF(?3, ''), handle),
               display_name = COALESCE(NULLIF(?4, ''), display_name),
               avatar_url = COALESCE(NULLIF(?5, ''), avatar_url),
               labels = ?6
             WHERE did = ?2`
          ).bind(hide, p.did, p.handle || '', p.displayName || '', avatarCid, JSON.stringify(p.labels || []))
        );
      }
      if (stmts.length > 0) {
        const batchResults = await db.batch(stmts);
        changed += batchResults.filter((r) => r.meta.changes > 0).length;
      }
    } catch {
      // best-effort — skip failures
    }
  }

  // save cursor at end of this page
  const lastRowid = results[results.length - 1].rowid;
  await env.KV.put("mod_cursor", String(lastRowid));
  console.log(JSON.stringify({ event: "moderation_refresh", checked, changed, cursor: lastRowid }));
}

import type { TursoDB } from "./db";
import type { Env } from "./types";
import { BSKY_TYPEAHEAD_URL } from "./types";
import { extractProfileFields } from "./utils";
import { getOverrides } from "./moderation";

// --- backfill: remove this block once at parity with Bluesky ---

export async function backfillFromBsky(
  term: string,
  limit: number,
  db: TursoDB,
): Promise<void> {
  try {
    const res = await fetch(
      `${BSKY_TYPEAHEAD_URL}?q=${encodeURIComponent(term)}&limit=${limit}`
    );
    if (!res.ok) return; // 429 or other error — just bail

    const data: any = await res.json();
    const actors: any[] = (data.actors || []).filter((a: any) => a.did);
    if (actors.length === 0) return;

    // drift detection: check which DIDs we already know about
    const placeholders = actors.map(() => '?').join(',');
    const existing = await db.prepare(
      `SELECT did FROM actors WHERE did IN (${placeholders})`
    ).bind(...actors.map(a => a.did)).all<{ did: string }>();
    const known = new Set((existing.results || []).map(r => r.did));

    // upsert all — fills in missing actors AND enriches existing ones
    // (e.g. actors ingested via Jetstream that lack avatar/displayName)
    const overrides = await getOverrides(db, actors.map((a) => a.did));
    const stmts = actors.map((a) => {
      const f = extractProfileFields(a, overrides.get(a.did) ?? null);
      return db.prepare(
        `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, labels, created_at, associated, followers_count, follows_count, posts_count, quality_score, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, unixepoch())
         ON CONFLICT(did) DO UPDATE SET
           handle = COALESCE(NULLIF(?2, ''), actors.handle),
           display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
           avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
           hidden = ?5,
           labels = ?6,
           created_at = COALESCE(NULLIF(?7, ''), actors.created_at),
           associated = COALESCE(NULLIF(?8, '{}'), actors.associated),
           followers_count = COALESCE(NULLIF(?9, 0), actors.followers_count),
           follows_count = COALESCE(NULLIF(?10, 0), actors.follows_count),
           posts_count = COALESCE(NULLIF(?11, 0), actors.posts_count),
           quality_score = COALESCE(NULLIF(?12, 0), actors.quality_score),
           updated_at = unixepoch()`
      ).bind(
        a.did, f.handle, f.displayName, f.avatarCid,
        f.hidden, f.labels, f.createdAt, f.associated,
        f.followersCount, f.followsCount, f.postsCount, f.qualityScore
      );
    });

    await db.batch(stmts);

    const discovered = actors.filter(a => !known.has(a.did)).length;
    console.log(JSON.stringify({ event: "backfill", term, upserted: actors.length, discovered }));
  } catch {
    // best-effort — don't let backfill errors affect anything
  }
}

export async function throttledBackfill(term: string, limit: number, db: TursoDB, env: Env): Promise<void> {
  // kill switch — set KV key "backfill" to "off" to disable without redeploying
  const flag = await env.KV.get("backfill");
  if (flag === "off") return;

  // global budget — cap total backfill triggers across all users
  const { success } = await env.RATE_LIMITER_STRICT.limit({ key: "backfill" });
  if (!success) {
    console.log(JSON.stringify({ event: "backfill_throttled", term }));
    return;
  }

  return backfillFromBsky(term, limit, db);
}

// --- end backfill ---

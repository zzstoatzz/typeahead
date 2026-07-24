import type { Stmt, TursoDB } from "./db";
import type { Env, SlingshotResponse } from "./types";
import { SLINGSHOT_URL, BSKY_GET_PROFILES_URL } from "./types";
import { extractProfileFields, fetchProfileFromPds } from "./utils";
import { getOverrides, getBlockedDomains, isDomainBlocked } from "./moderation";

/** record an actor-count snapshot for the current hour (idempotent).
 *
 *  the corpus is ~6M rows; a `COUNT(*)` / `SUM(CASE …)` over the whole table
 *  takes ~300s over libSQL HTTP, which silently blew the cron's budget once the
 *  table grew (snapshots froze 2026-05-18). instead:
 *    - `total`        — O(1) read of the maintained `actors_total` counter
 *    - `hidden`       — selective count via `idx_actors_hidden` (partial index)
 *    - empty handles  — selective range seek on `idx_actors_handle`
 *    - empty avatars  — selective count via `idx_actors_avatar_empty`
 *  with_handles / with_avatars are then `total - empty`. every count touches
 *  only a small slice (<400k matching rows), so the whole thing is sub-second. */
export async function recordSnapshot(db: TursoDB): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  const [totalRes, hiddenRes, emptyHandlesRes, emptyAvatarsRes] = await db.batch([
    db.prepare("SELECT value AS n FROM counters WHERE name = 'actors_total'"),
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE hidden != 0"),
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE handle = ''"),
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE avatar_url = ''"),
  ], "read");

  const num = (r: any): number => Number(r?.results?.[0]?.n ?? 0);
  const total = num(totalRes);
  if (total <= 0) return; // counter not seeded yet — don't write a bogus zero row

  const hidden = num(hiddenRes);
  const withHandles = total - num(emptyHandlesRes);
  const withAvatars = total - num(emptyAvatarsRes);

  await db.prepare(
    `INSERT OR REPLACE INTO snapshots (hour, total, with_handles, with_avatars, hidden)
     VALUES (?1, ?2, ?3, ?4, ?5)`
  )
    .bind(hour, total - hidden, withHandles, withAvatars, hidden)
    .run();
}

/** materialize stats data to KV so /stats never queries turso on the request path */
export async function materializeStats(db: TursoDB, env: Env): Promise<void> {
  // metrics: 5-min buckets. 100k buckets ≈ 1 year of headroom — effectively
  // "all" for our current age. lets the /stats range selector compute true totals.
  // snapshots: hourly. 5000 covers ~7 months.
  // deltas: 5-min, 288 = 24h (used to stitch trend chart tail).
  // traffic: last 30d of per-domain 5-min buckets, rolled up to hourly.
  // bounded by hours × active domains (≤720 × ~dozens), NOT the corpus.
  const trafficSince = Math.floor(Date.now() / 300_000) - 30 * 288;
  const [metricsRes, snapshotRes, deltasRes, trafficRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics ORDER BY hour DESC LIMIT 100000"),
    db.prepare("SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots ORDER BY hour ASC LIMIT 5000"),
    db.prepare("SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, SUM(hits) AS hits
       FROM traffic_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain ORDER BY hour ASC`
    ).bind(trafficSince),
  ], "read");

  await env.KV.put("stats_data", JSON.stringify({
    metrics: metricsRes.results ?? [],
    snapshots: snapshotRes.results ?? [],
    deltas: deltasRes.results ?? [],
    traffic_series: trafficRes.results ?? [],
    materialized_at: Date.now(),
  }));

  // retention: drop per-domain buckets older than 90d (PK-prefix range delete)
  const pruneBefore = Math.floor(Date.now() / 300_000) - 90 * 288;
  await db.prepare("DELETE FROM traffic_buckets WHERE bucket < ?1").bind(pruneBefore).run();
}

/** lease-coordinated, two-phase enrichment: slingshot identity + getProfiles batch.
 *  returns { resolved, enriched } counts for delta tracking. */
export async function enrichActors(db: TursoDB, env: Env): Promise<{ resolved: number; enriched: number }> {
  // lease via KV — 30s TTL, skip if another run is active
  const existing = await env.KV.get("enrich_lock");
  if (existing) return { resolved: 0, enriched: 0 };
  await env.KV.put("enrich_lock", "1", { expirationTtl: 30 });

  let totalResolved = 0;
  let totalEnriched = 0;

  try {
    // phase 1: identity resolution via slingshot
    const { results: identityRows } = await db.prepare(
      `SELECT did FROM actors
       WHERE handle = '' AND identity_checked_at < unixepoch() - 3600
       ORDER BY identity_checked_at ASC LIMIT 500`
    ).all<{ did: string }>();

    if (identityRows && identityRows.length > 0) {
      let resolved = 0;
      const BATCH = 20;
      for (let i = 0; i < identityRows.length; i += BATCH) {
        const batch = identityRows.slice(i, i + BATCH);
        await Promise.all(batch.map(async ({ did }) => {
          try {
            const res = await fetch(
              `${SLINGSHOT_URL}?identifier=${encodeURIComponent(did)}`
            );
            if (!res.ok) {
              // mark attempt so we back off for 1hr
              await db.prepare(
                "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1"
              ).bind(did).run();
              return;
            }
            const identity: SlingshotResponse = await res.json();
            await db.prepare(
              `UPDATE actors SET handle = COALESCE(NULLIF(?1, ''), handle),
                 pds = COALESCE(NULLIF(?2, ''), pds),
                 identity_checked_at = unixepoch(),
                 updated_at = unixepoch()
               WHERE did = ?3`
            ).bind(identity.handle || '', identity.pds || '', did).run();
            if (identity.handle) resolved++;
          } catch {
            await db.prepare(
              "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1"
            ).bind(did).run().catch(() => {});
          }
        }));
      }
      totalResolved = resolved;
      if (resolved > 0) {
        console.log(JSON.stringify({ event: "enrich_identity", resolved, checked: identityRows.length }));
      }
    }

    // phase 1b: backfill PDS for actors that have handles but no PDS
    const { results: pdsRows } = await db.prepare(
      `SELECT did FROM actors
       WHERE handle != '' AND pds = ''
         AND identity_checked_at < unixepoch() - 86400
       ORDER BY identity_checked_at ASC LIMIT 500`
    ).all<{ did: string }>();

    if (pdsRows && pdsRows.length > 0) {
      let filled = 0;
      const BATCH = 20;
      for (let i = 0; i < pdsRows.length; i += BATCH) {
        const batch = pdsRows.slice(i, i + BATCH);
        await Promise.all(batch.map(async ({ did }) => {
          try {
            const res = await fetch(
              `${SLINGSHOT_URL}?identifier=${encodeURIComponent(did)}`
            );
            if (!res.ok) {
              await db.prepare(
                "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1"
              ).bind(did).run();
              return;
            }
            const identity: SlingshotResponse = await res.json();
            if (identity.pds) {
              await db.prepare(
                "UPDATE actors SET pds = ?1, identity_checked_at = unixepoch(), updated_at = unixepoch() WHERE did = ?2"
              ).bind(identity.pds, did).run();
              filled++;
            } else {
              await db.prepare(
                "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1"
              ).bind(did).run();
            }
          } catch {
            await db.prepare(
              "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1"
            ).bind(did).run().catch(() => {});
          }
        }));
      }
      if (filled > 0) {
        console.log(JSON.stringify({ event: "enrich_pds", filled, checked: pdsRows.length }));
      }
    }

    // phase 2: profile + labels enrichment via getProfiles batch
    // targets actors missing avatar OR labels — one API call per 25 actors
    const { results: profileRows } = await db.prepare(
      `SELECT did, handle, pds FROM actors
       WHERE handle != ''
         AND (avatar_url = '' OR labels = '[]' OR created_at = '')
         AND profile_checked_at < unixepoch() - 3600
       ORDER BY profile_checked_at ASC LIMIT 250`
    ).all<{ did: string; handle: string; pds: string }>();

    if (profileRows && profileRows.length > 0) {
      const overrides = await getOverrides(db, profileRows.map((r) => r.did));
      const blockedDomains = await getBlockedDomains(db);
      let enriched = 0;
      for (let i = 0; i < profileRows.length; i += 25) {
        const batch = profileRows.slice(i, i + 25);
        const params = batch.map((r) => `actors=${encodeURIComponent(r.did)}`).join("&");
        try {
          if (i > 0) await new Promise((r) => setTimeout(r, 200));
          const res = await fetch(`${BSKY_GET_PROFILES_URL}?${params}`);
          if (res.status === 429) {
            console.log(JSON.stringify({ event: "enrich_profile", status: "rate_limited", enriched }));
            break;
          }
          if (!res.ok) continue;

          const data: any = await res.json();
          const profiles: any[] = data.profiles || [];

          const stmts: Stmt[] = [];
          const returned = new Set<string>();
          for (const p of profiles) {
            returned.add(p.did);
            const f = extractProfileFields(p, overrides.get(p.did) ?? null, isDomainBlocked(p.handle || "", blockedDomains));
            stmts.push(
              db.prepare(
                `UPDATE actors SET
                   handle = COALESCE(NULLIF(?2, ''), handle),
                   display_name = COALESCE(NULLIF(?3, ''), display_name),
                   avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
                   labels = ?5, hidden = ?6,
                   created_at = COALESCE(NULLIF(?7, ''), created_at),
                   associated = COALESCE(NULLIF(?8, '{}'), associated),
                   followers_count = COALESCE(NULLIF(?9, 0), followers_count),
                   follows_count = COALESCE(NULLIF(?10, 0), follows_count),
                   posts_count = COALESCE(NULLIF(?11, 0), posts_count),
                   quality_score = COALESCE(NULLIF(?12, 0), quality_score),
                   profile_checked_at = unixepoch(),
                   updated_at = unixepoch()
                 WHERE did = ?1`
              ).bind(
                p.did, f.handle, f.displayName,
                f.avatarCid, f.labels, f.hidden,
                f.createdAt, f.associated,
                f.followersCount, f.followsCount, f.postsCount, f.qualityScore
              )
            );
            if (f.avatarCid) enriched++;
          }
          // PDS fallback for unreturned DIDs
          for (const r of batch) {
            if (returned.has(r.did)) continue;
            const override = overrides.get(r.did) ?? null;
            if (r.pds && (override === 'show' || !override)) {
              // non-bsky PDS (e.g. tngl.sh): always try PDS fallback directly
              const isNonBsky = !r.pds.includes('.bsky.network');
              let tryPds = isNonBsky;
              if (!tryPds) {
                // bsky PDS: check if this is a takedown
                if (override === 'show') {
                  tryPds = true;
                } else {
                  try {
                    const probe = await fetch(
                      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(r.did)}`
                    );
                    if (!probe.ok) {
                      const err: any = await probe.json().catch(() => ({}));
                      tryPds = err.error === 'AccountTakedown';
                    }
                  } catch {}
                }
              }
              if (tryPds) {
                const pdsProfile = await fetchProfileFromPds(r.did, r.pds);
                if (pdsProfile) {
                  const f = extractProfileFields(pdsProfile, override, isDomainBlocked(r.handle || "", blockedDomains));
                  stmts.push(
                    db.prepare(
                      `UPDATE actors SET
                         display_name = COALESCE(NULLIF(?2, ''), display_name),
                         avatar_url = COALESCE(NULLIF(?3, ''), avatar_url),
                         labels = ?4, hidden = ?5,
                         created_at = COALESCE(NULLIF(?6, ''), created_at),
                         associated = COALESCE(NULLIF(?7, '{}'), associated),
                         profile_checked_at = unixepoch(),
                         updated_at = unixepoch()
                       WHERE did = ?1`
                    ).bind(r.did, f.displayName, f.avatarCid, f.labels, f.hidden, f.createdAt, f.associated)
                  );
                  if (f.avatarCid) enriched++;
                  continue;
                }
              }
            }
            stmts.push(
              db.prepare(
                "UPDATE actors SET profile_checked_at = unixepoch() WHERE did = ?1"
              ).bind(r.did)
            );
          }
          if (stmts.length > 0) await db.batch(stmts);
        } catch {
          // best-effort — skip failures
        }
      }
      totalEnriched = enriched;
      if (enriched > 0) {
        console.log(JSON.stringify({ event: "enrich_profile", enriched, checked: profileRows.length }));
      }
    }
  } finally {
    await env.KV.delete("enrich_lock").catch(() => {});
  }

  return { resolved: totalResolved, enriched: totalEnriched };
}

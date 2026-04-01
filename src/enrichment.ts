import type { Stmt, TursoDB } from "./db";
import type { Env, SlingshotResponse } from "./types";
import { SLINGSHOT_URL, BSKY_GET_PROFILES_URL } from "./types";
import { extractProfileFields, fetchProfileFromPds } from "./utils";
import { getOverrides } from "./moderation";

/** record an actor-count snapshot for the current hour (idempotent) */
export async function recordSnapshot(db: TursoDB): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  const row = await db.prepare(
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN handle != '' THEN 1 ELSE 0 END) AS with_handles,
            SUM(CASE WHEN avatar_url != '' THEN 1 ELSE 0 END) AS with_avatars,
            SUM(CASE WHEN hidden != 0 THEN 1 ELSE 0 END) AS hidden
     FROM actors`
  ).first<{ total: number; with_handles: number; with_avatars: number; hidden: number }>();
  if (row) {
    await db.prepare(
      `INSERT OR REPLACE INTO snapshots (hour, total, with_handles, with_avatars, hidden)
       VALUES (?1, ?2, ?3, ?4, ?5)`
    )
      .bind(hour, row.total - (row.hidden ?? 0), row.with_handles, row.with_avatars, row.hidden ?? 0)
      .run();
  }
}

/** materialize stats data to KV so /stats never queries turso on the request path */
export async function materializeStats(db: TursoDB, env: Env): Promise<void> {
  const [metricsRes, snapshotRes, deltasRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics ORDER BY hour DESC LIMIT 2016"),
    db.prepare("SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots ORDER BY hour ASC LIMIT 2000"),
    db.prepare("SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"),
  ], "read");

  await env.KV.put("stats_data", JSON.stringify({
    metrics: metricsRes.results ?? [],
    snapshots: snapshotRes.results ?? [],
    deltas: deltasRes.results ?? [],
    materialized_at: Date.now(),
  }));
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
       ORDER BY identity_checked_at ASC LIMIT 100`
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
                 identity_checked_at = unixepoch()
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
       ORDER BY identity_checked_at ASC LIMIT 100`
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
                "UPDATE actors SET pds = ?1, identity_checked_at = unixepoch() WHERE did = ?2"
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
      `SELECT did, pds FROM actors
       WHERE handle != ''
         AND (avatar_url = '' OR labels = '[]' OR created_at = '')
         AND profile_checked_at < unixepoch() - 3600
       ORDER BY profile_checked_at ASC LIMIT 75`
    ).all<{ did: string; pds: string }>();

    if (profileRows && profileRows.length > 0) {
      const overrides = await getOverrides(db, profileRows.map((r) => r.did));
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
            const f = extractProfileFields(p, overrides.get(p.did) ?? null);
            stmts.push(
              db.prepare(
                `UPDATE actors SET
                   handle = COALESCE(NULLIF(?2, ''), handle),
                   display_name = COALESCE(NULLIF(?3, ''), display_name),
                   avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
                   labels = ?5, hidden = ?6,
                   created_at = COALESCE(NULLIF(?7, ''), created_at),
                   associated = COALESCE(NULLIF(?8, '{}'), associated),
                   profile_checked_at = unixepoch()
                 WHERE did = ?1`
              ).bind(
                p.did, f.handle, f.displayName,
                f.avatarCid, f.labels, f.hidden,
                f.createdAt, f.associated
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
                  const f = extractProfileFields(pdsProfile, override);
                  stmts.push(
                    db.prepare(
                      `UPDATE actors SET
                         display_name = COALESCE(NULLIF(?2, ''), display_name),
                         avatar_url = COALESCE(NULLIF(?3, ''), avatar_url),
                         labels = ?4, hidden = ?5,
                         created_at = COALESCE(NULLIF(?6, ''), created_at),
                         associated = COALESCE(NULLIF(?7, '{}'), associated),
                         profile_checked_at = unixepoch()
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

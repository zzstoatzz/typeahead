import type { Stmt, TursoDB } from "./db";
import type { Env } from "./types";
import { BSKY_GET_PROFILES_URL } from "./types";
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
    // COLLATE NOCASE or this is a SCAN of all ~10.6M index entries instead of a
    // SEARCH: idx_actors_handle is declared COLLATE NOCASE, so a binary-equality
    // predicate cannot seek it and falls back to idx_actors_enrich_identity as a
    // covering scan. Cheap when idle, but it is the most expensive query in the
    // hourly cron and the first to time out under write contention — which
    // fails the step and leaves that hour with no snapshot row.
    db.prepare("SELECT COUNT(*) AS n FROM actors WHERE handle = '' COLLATE NOCASE"),
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
  const traffic5mSince = Math.floor(Date.now() / 300_000) - 2 * 288; // raw 5-min, last 48h
  const [metricsRes, snapshotRes, deltasRes, trafficRes, traffic5mRes] = await db.batch([
    db.prepare("SELECT hour, searches, total_ms, cache_hits, cache_ms FROM metrics ORDER BY hour DESC LIMIT 100000"),
    db.prepare("SELECT hour, total, with_handles, with_avatars, hidden FROM snapshots ORDER BY hour ASC LIMIT 5000"),
    db.prepare("SELECT bucket, actors_delta, handles_delta, avatars_delta FROM actor_deltas ORDER BY bucket DESC LIMIT 288"),
    db.prepare(
      `SELECT bucket / 12 AS hour, domain, SUM(hits) AS hits
       FROM traffic_buckets WHERE bucket >= ?1
       GROUP BY bucket / 12, domain ORDER BY hour ASC`
    ).bind(trafficSince),
    db.prepare(
      `SELECT bucket, domain, hits FROM traffic_buckets
       WHERE bucket >= ?1 ORDER BY bucket ASC`
    ).bind(traffic5mSince),
  ], "read");

  await env.KV.put("stats_data", JSON.stringify({
    metrics: metricsRes.results ?? [],
    snapshots: snapshotRes.results ?? [],
    deltas: deltasRes.results ?? [],
    traffic_series: trafficRes.results ?? [],
    traffic_series_5m: traffic5mRes.results ?? [],
    materialized_at: Date.now(),
  }));

  // retention: drop per-domain buckets older than 90d (PK-prefix range delete)
  const pruneBefore = Math.floor(Date.now() / 300_000) - 90 * 288;
  await db.prepare("DELETE FROM traffic_buckets WHERE bucket < ?1").bind(pruneBefore).run();
}

/** 25 DIDs per getProfiles call, so this is 200 requests per run.
 *
 *  Sized against ARRIVALS, not the backlog. The relay firehose discovers ~2,900
 *  new accounts/hour network-wide; at the previous 2,500/run this cron ran just
 *  under that and the handle backlog grew rather than shrank (1,312,203 →
 *  1,314,130 over 8h on 2026-07-28). 5,000 clears arrivals with room to spare.
 *
 *  Draining the ~1.3M historical backlog is NOT this job — that is
 *  scripts/plc-identity-sync.py, which does it in one pass from the PLC log. */
const IDENTITY_LIMIT = 5000;

/** Lease-coordinated enrichment. Both phases now batch through getProfiles;
 *  the per-DID slingshot paths they replaced could not drain their backlogs.
 *  Returns { resolved, enriched } counts for delta tracking. */
export async function enrichActors(db: TursoDB, env: Env): Promise<{ resolved: number; enriched: number }> {
  // lease via KV — 30s TTL, skip if another run is active
  // TTL must exceed the real run duration or the lock silently stops working.
  // At 30s it already did: phase 1 alone is ~200 requests and outlives it.
  // Hourly cron means nothing else contends today, but a manual
  // /admin/enrich-profiles during a run would have.
  const existing = await env.KV.get("enrich_lock");
  if (existing) return { resolved: 0, enriched: 0 };
  // 900s: both phases are now 200 getProfiles calls each (~500ms/call with the
  // pacing gap) plus the budgeted PDS fallbacks — a full run can pass 5 minutes.
  await env.KV.put("enrich_lock", "1", { expirationTtl: 900 });

  let totalResolved = 0;
  let totalEnriched = 0;

  try {
    // phase 1: identity resolution, 25 DIDs per request via getProfiles.
    //
    // This was one slingshot call PER DID, capped at 500/run — 500/hour against
    // a backlog that reached 1,311,817 rows on 2026-07-28, i.e. ~109 days to
    // drain, with every row at identity_checked_at=0 so the ORDER BY tiebreak
    // was arbitrary and a given DID might never surface. getProfiles takes 25
    // per call at ~154 DIDs/s with a 96% hit rate (measured against that real
    // backlog) and returns handle, display name, avatar and counts in the same
    // response, so it feeds phase 2 as well.
    //
    // The ~4% getProfiles cannot resolve are accounts the appview never indexed
    // — self-hosted PDSes, the birds.place class. They fall through to
    // scripts/plc-identity-sync.py, which reads the PLC log directly.
    const { results: identityRows } = await db.prepare(
      `SELECT did FROM actors
       WHERE handle = '' AND identity_checked_at < unixepoch() - 3600
       ORDER BY identity_checked_at ASC LIMIT ${IDENTITY_LIMIT}`
    ).all<{ did: string }>();

    if (identityRows && identityRows.length > 0) {
      let resolved = 0;
      for (let i = 0; i < identityRows.length; i += 25) {
        const batch = identityRows.slice(i, i + 25);
        const params = batch.map((r) => `actors=${encodeURIComponent(r.did)}`).join("&");
        const stmts: Stmt[] = [];
        try {
          if (i > 0) await new Promise((r) => setTimeout(r, 100));
          const res = await fetch(`${BSKY_GET_PROFILES_URL}?${params}`);
          if (res.status === 429) {
            console.log(JSON.stringify({ event: "enrich_identity", status: "rate_limited", resolved }));
            break;
          }
          if (res.ok) {
            const data: any = await res.json();
            for (const p of data.profiles || []) {
              if (!p.handle || p.handle === "handle.invalid") continue;
              stmts.push(
                db.prepare(
                  `UPDATE actors SET handle = ?2,
                     identity_checked_at = unixepoch(), updated_at = unixepoch()
                   WHERE did = ?1`
                ).bind(p.did, p.handle)
              );
              resolved++;
            }
          }
        } catch {
          // fall through — the stamp below still applies so we back off
        }
        // stamp every DID in the batch, resolved or not, so a permanently
        // unresolvable DID cannot pin the queue and starve the rest.
        for (const { did } of batch) {
          stmts.push(
            db.prepare(
              "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1 AND handle = ''"
            ).bind(did)
          );
        }
        if (stmts.length > 0) await db.batch(stmts);
      }
      totalResolved = resolved;
      if (resolved > 0) {
        console.log(JSON.stringify({ event: "enrich_identity", resolved, checked: identityRows.length }));
      }
    }

    // phase 1b (per-DID slingshot pds backfill) is DELETED, not disabled.
    // It resolved one DID per request at 500/run against a 9,315,512-row
    // backlog — 212 years to drain, so it was never going to converge and its
    // presence disguised that. pds now arrives two ways instead:
    //   - new actors: the ingester's slingshot pool already resolves pds
    //     alongside handle and now posts it (services/src/identity.zig).
    //   - the historical backlog: scripts/plc-identity-sync.py, which reads the
    //     PLC log in bulk rather than one HTTP request per DID.

    // phase 2: profile + labels enrichment via getProfiles batch
    // targets actors missing avatar OR labels — one API call per 25 actors
    //
    // Sized against ARRIVALS like phase 1: the firehose discovers ~2,900 new
    // actors/hour and every one of them enters this cohort (avatar_url='',
    // labels='[]'). At the previous 250/run the backlog GREW ~2,650/hour by
    // construction — by 2026-08-15 several million actors had never been
    // profile-checked, which is what a 61.9%-and-flat avatar coverage and a
    // near-zero hidden count on /stats actually measured. avatar coverage
    // among CHECKED actors is ~95%.
    const { results: profileRows } = await db.prepare(
      `SELECT did, handle, pds FROM actors
       WHERE handle != ''
         AND (avatar_url = '' OR labels = '[]' OR created_at = '')
         AND profile_checked_at < unixepoch() - 3600
       ORDER BY profile_checked_at ASC LIMIT 5000`
    ).all<{ did: string; handle: string; pds: string }>();

    if (profileRows && profileRows.length > 0) {
      const overrides = await getOverrides(db, profileRows.map((r) => r.did));
      const blockedDomains = await getBlockedDomains(db);
      let enriched = 0;
      // The PDS fallback costs up to two EXTRA fetches per DID getProfiles
      // didn't return, and the never-checked backlog is dead-account-heavy —
      // uncapped it could add thousands of serial requests per run. Budgeted:
      // past the cap, unreturned DIDs just get profile_checked_at stamped and
      // the fallback catches them on a later pass.
      let pdsFallbackBudget = 100;
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
            if (r.pds && (override === 'show' || !override) && pdsFallbackBudget-- > 0) {
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

import type { Stmt, TursoDB } from "../db";
import type { Env, SlingshotResponse } from "../types";
import { SLINGSHOT_URL, BSKY_GET_PROFILES_URL } from "../types";
import { clientIP, json, extractProfileFields, fetchProfileFromPds } from "../utils";
import { getOverride, getOverrides, resolveHidden, isDomainBlocked, getBlockedDomains } from "../moderation";
import { recordActorDelta } from "../metrics";

export async function handleDelete(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const auth = request.headers.get("Authorization");
  if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
    const ip = clientIP(request);
    await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
    console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/delete", ip }));
    return json({ error: "unauthorized" }, 401);
  }

  let body: { dids: string[] };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const { dids } = body;
  if (!Array.isArray(dids) || dids.length === 0) {
    return json({ error: "dids must be a non-empty array" }, 400);
  }
  if (dids.length > 10_000) {
    return json({ error: "batch too large (max 10000)" }, 400);
  }

  // chunk into batches of 200 with short pauses to let reads through
  const CHUNK = 200;
  let actualDeletes = 0;
  const t0 = Date.now();

  for (let i = 0; i < dids.length; i += CHUNK) {
    if (i > 0) await new Promise((r) => setTimeout(r, 100));
    const chunk = dids.slice(i, i + CHUNK);
    const stmts = chunk.flatMap((did) => [
      db.prepare("DELETE FROM actors WHERE did = ?1").bind(did),
      db.prepare("INSERT OR REPLACE INTO tombstones (did, deleted_at) VALUES (?1, unixepoch())").bind(did),
    ]);
    const batchResults = await db.batch(stmts);
    actualDeletes += batchResults
      .filter((_, j) => j % 2 === 0)
      .reduce((s, r) => s + r.meta.changes, 0);
  }

  console.log(JSON.stringify({
    event: "admin_delete", requested: dids.length, deleted: actualDeletes,
    chunks: Math.ceil(dids.length / CHUNK), ms: Date.now() - t0,
  }));

  if (actualDeletes > 0) {
    ctx.waitUntil(recordActorDelta(db, { actors: -actualDeletes }));
  }

  return json({ ok: true, deleted: actualDeletes });
}

export async function handleCursor(
  request: Request,
  env: Env
): Promise<Response> {
  const auth = request.headers.get("Authorization");
  if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
    const ip = clientIP(request);
    await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
    console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/cursor", ip }));
    return json({ error: "unauthorized" }, 401);
  }

  // relay_cursor holds a relay `seq`, not jetstream's µs timestamp. The keys are
  // deliberately separate: feeding the old µs value to subscribeRepos would
  // request a sequence ~7 orders of magnitude in the future. A missing key means
  // "start live", which is the intended state on first boot after the cutover.
  const [cursor, cronAttempt] = await Promise.all([
    env.KV.get("relay_cursor"),
    env.KV.get("cron_last_attempt"),
  ]);
  // cron_last_attempt distinguishes "the schedule did not fire" from "a step
  // failed" when an hour is missing from `snapshots`.
  return json({
    cursor: cursor ? Number(cursor) : null,
    cron_last_attempt: cronAttempt ? Number(cronAttempt) : null,
  });
}

/** resolve a handle or DID via slingshot, then upsert into the active DB */
export async function handleRequestIndexing(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const identifier =
    url.searchParams.get("handle") ||
    url.searchParams.get("did") ||
    "";

  if (!identifier) {
    return json({ error: "enter a handle or DID to request indexing." }, 400);
  }

  // Phase timings, reported as Server-Timing. This endpoint's latency has
  // ranged 0.83s-7.11s in measurement and the variance is not in one obvious
  // place; guessing at it produced two wrong fixes already.
  const t: Record<string, number> = {};
  let mark = Date.now();
  const lap = (name: string) => { t[name] = Date.now() - mark; mark = Date.now(); };

  // resolve via slingshot
  const res = await fetch(
    `${SLINGSHOT_URL}?identifier=${encodeURIComponent(identifier)}`
  );
  if (!res.ok) {
    return json({ error: `could not resolve "${identifier}". check that it's a valid handle or DID.` }, 404);
  }

  const identity: SlingshotResponse = await res.json();
  lap("slingshot");

  // fetch profile from public API for display name + avatar + labels + metadata
  let f = {
    displayName: '', avatarCid: '', hidden: 0, labels: '[]', createdAt: '', associated: '{}',
    followersCount: 0, followsCount: 0, postsCount: 0, qualityScore: 0,
  };
  const override = await getOverride(db, identity.did);
  lap("override");
  const domainBlocked = isDomainBlocked(identity.handle || "", await getBlockedDomains(db));
  lap("blocked_domains");
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
    lap("bsky_profile");
    if (profileRes.ok) {
      f = extractProfileFields(await profileRes.json(), override, domainBlocked);
    } else if (override === 'show' && identity.pds) {
      const pdsProfile = await fetchProfileFromPds(identity.did, identity.pds);
      if (pdsProfile) f = extractProfileFields(pdsProfile, override, domainBlocked);
    }
  } catch {
    // profile enrichment is best-effort
  }

  await db.prepare(
    `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, labels, created_at, associated, pds, followers_count, follows_count, posts_count, quality_score, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, unixepoch())
     ON CONFLICT(did) DO UPDATE SET
       handle = ?2,
       display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
       avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
       hidden = ?5,
       labels = ?6,
       created_at = COALESCE(NULLIF(?7, ''), actors.created_at),
       associated = COALESCE(NULLIF(?8, '{}'), actors.associated),
       pds = COALESCE(NULLIF(?9, ''), actors.pds),
       followers_count = COALESCE(NULLIF(?10, 0), actors.followers_count),
       follows_count = COALESCE(NULLIF(?11, 0), actors.follows_count),
       posts_count = COALESCE(NULLIF(?12, 0), actors.posts_count),
       quality_score = COALESCE(NULLIF(?13, 0), actors.quality_score),
       updated_at = unixepoch()`
  )
    .bind(identity.did, identity.handle, f.displayName, f.avatarCid, f.hidden, f.labels, f.createdAt, f.associated, identity.pds || '', f.followersCount, f.followsCount, f.postsCount, f.qualityScore)
    .run();
  lap("turso_write");

  // Push it into the search replica NOW rather than waiting for the next sync
  // tick. Turso is the authority and the row is already committed above; this
  // just tells the replica to pick that one row up immediately, so someone who
  // explicitly asked us to index an account can find it seconds later instead
  // of up to 5 minutes — or, when sync stalls, not at all (2026-07-28: 48h).
  //
  // Fire-and-forget: the sync loop is still the mechanism of record, so a
  // failure here costs freshness, never correctness.
  ctx.waitUntil(pushToReplica(env, {
    did: identity.did,
    handle: identity.handle,
    display_name: f.displayName,
    avatar_url: f.avatarCid,
    hidden: f.hidden,
    labels: f.labels,
    created_at: f.createdAt,
    associated: f.associated,
    pds: identity.pds || "",
    followers_count: f.followersCount,
    follows_count: f.followsCount,
    posts_count: f.postsCount,
    quality_score: f.qualityScore,
    updated_at: Math.floor(Date.now() / 1000),
  }));

  const timing = Object.entries(t).map(([k, v]) => `${k};dur=${v}`).join(", ");
  return json({
    handle: identity.handle,
    did: identity.did,
    ...(f.hidden ? { hidden: true, reason: "hidden by moderation" } : { hidden: false }),
  }, 200, { "Server-Timing": timing });
}

/** Hand the search replica the row we just wrote, so it is searchable now.
 *
 *  Sends the ROW, not just the DID. The first version sent a DID and made the
 *  search service read it back out of Turso — which routed the fast path
 *  through the most contended component in the system. Measured 2026-07-31
 *  with a bulk job running: an indexed point lookup took 28s and this hung.
 *  We already have every field; there is nothing to look up.
 *
 *  Fire-and-forget. The 5-minute sync remains the mechanism of record, so a
 *  failure here costs freshness for one actor, never correctness.
 */
async function pushToReplica(env: Env, row: Record<string, unknown>): Promise<void> {
  if (!env.SEARCH_BACKEND_URL || !env.ADMIN_SECRET) return;
  const t0 = Date.now();
  try {
    const r = await fetch(`${env.SEARCH_BACKEND_URL}/admin/refresh`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.ADMIN_SECRET}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(row),
      signal: AbortSignal.timeout(5000),
    });
    console.log(JSON.stringify({
      event: "replica_push", did: row.did, status: r.status, ms: Date.now() - t0,
    }));
  } catch (e: any) {
    console.log(JSON.stringify({
      event: "replica_push_failed", did: row.did, error: e?.message, ms: Date.now() - t0,
    }));
  }
}

function authCheck(request: Request, env: Env): Response | null {
  const auth = request.headers.get("Authorization");
  if (auth === `Bearer ${env.ADMIN_SECRET}`) return null;
  const ip = clientIP(request);
  console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/mod-override", ip }));
  return json({ error: "unauthorized" }, 401);
}

export async function handleModOverrideSet(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let body: { did: string; action: string; reason?: string };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const { did, action, reason } = body;
  if (!did || (action !== 'show' && action !== 'hide')) {
    return json({ error: "did required, action must be 'show' or 'hide'" }, 400);
  }

  // upsert override
  await db.prepare(
    `INSERT INTO mod_overrides (did, action, reason) VALUES (?1, ?2, ?3)
     ON CONFLICT(did) DO UPDATE SET action = ?2, reason = ?3, created_at = unixepoch()`
  ).bind(did, action, reason || '').run();

  // immediately apply to actors table (touch updated_at so fly replica syncs)
  const hidden = action === 'show' ? 0 : 1;
  await db.prepare(
    "UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2"
  ).bind(hidden, did).run();

  return json({ did, action, reason: reason || '', hidden });
}

export async function handleModOverrideDelete(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const did = url.searchParams.get("did") || "";
  if (!did) return json({ error: "did query param required" }, 400);

  await db.prepare("DELETE FROM mod_overrides WHERE did = ?1").bind(did).run();

  // recompute effective hidden from ALL sources now that the override is gone
  // (labels + slur + domain block) — not labels alone, or a domain-blocked
  // account would resurface the moment its show/hide override is removed.
  const row = await db.prepare(
    "SELECT handle, labels FROM actors WHERE did = ?1"
  ).bind(did).first<{ handle: string; labels: string }>();

  let hidden = 0;
  if (row) {
    let labels: any[] = [];
    try { labels = JSON.parse(row.labels); } catch { labels = []; }
    const blockedDomains = await getBlockedDomains(db);
    hidden = resolveHidden({
      labels,
      handle: row.handle,
      override: null,
      domainBlocked: isDomainBlocked(row.handle || "", blockedDomains),
    });
    await db.prepare("UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2").bind(hidden, did).run();
  }

  return json({ did, hidden });
}

export async function handleModOverrideList(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const { results } = await db.prepare(
    "SELECT did, action, reason, created_at FROM mod_overrides ORDER BY created_at DESC"
  ).all<{ did: string; action: string; reason: string; created_at: number }>();

  return json({ overrides: results || [] });
}

/** normalize an input domain: lowercase, trim, strip a leading "*." / "." */
function normalizeDomain(raw: string): string {
  return (raw || "").trim().toLowerCase().replace(/^\*?\.?/, "");
}

// rowid-window size for domain backfills. The `handle LIKE '%.domain'` match
// is a full table scan (leading wildcard can't use the handle index), so a
// single UPDATE holds the SQLite single-writer lock for ~8s over 5.9M rows,
// blocking ALL reads and timing out the client. Windowing bounds each
// transaction to ~WINDOW rows → short locks reads can interleave with, run in
// the background so the client returns immediately.
const BACKFILL_WINDOW = 500_000;

async function maxRowid(db: TursoDB): Promise<number> {
  const row = await db.prepare("SELECT max(rowid) AS m FROM actors").first<{ m: number | null }>();
  return row?.m ?? 0;
}

/** Hide apex + every subdomain of `domain`, sparing 'show'-override DIDs, in
 *  bounded rowid windows (short write locks). Only flips 0→1 so already-hidden
 *  rows don't churn updated_at / the search service sync. */
async function backfillDomainBlock(db: TursoDB, domain: string): Promise<void> {
  const suffix = "%." + domain;
  const max = await maxRowid(db);
  let cursor = 0;
  let hidden = 0;
  while (cursor < max) {
    const res = await db.prepare(
      `UPDATE actors SET hidden = 1, updated_at = unixepoch()
       WHERE rowid > ?1 AND rowid <= ?2
         AND (handle = ?3 OR handle LIKE ?4) COLLATE NOCASE
         AND hidden = 0
         AND did NOT IN (SELECT did FROM mod_overrides WHERE action = 'show')`
    ).bind(cursor, cursor + BACKFILL_WINDOW, domain, suffix).run();
    hidden += res.meta?.changes ?? 0;
    cursor += BACKFILL_WINDOW;
  }
  console.log(JSON.stringify({ event: "domain_block_backfill", domain, hidden }));
}

export async function handleDomainBlockSet(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let body: { domain: string; reason?: string };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const domain = normalizeDomain(body.domain);
  if (!domain || !domain.includes(".") || /\s/.test(domain)) {
    return json({ error: "valid domain required (e.g. dadavidtseng.com)" }, 400);
  }

  // record the block synchronously (fast, indexed PK) — new accounts are then
  // caught by the enrich/cron paths via resolveHidden regardless of backfill.
  await db.prepare(
    `INSERT INTO blocked_domains (domain, reason) VALUES (?1, ?2)
     ON CONFLICT(domain) DO UPDATE SET reason = ?2, created_at = unixepoch()`
  ).bind(domain, body.reason || "").run();

  // backfill existing matches in the background so the client never blocks on
  // the scan. waitUntil keeps the worker alive past the response; the cron's
  // resolveHidden pass is the backstop if it doesn't finish.
  ctx.waitUntil(backfillDomainBlock(db, domain));

  return json({ domain, reason: body.reason || "", status: "blocked; backfilling existing matches in background" });
}

/** After unblocking `domain`, recompute effective hidden for every actor that
 *  matched it, from all REMAINING sources (labels + slur + other domain blocks
 *  + override) — so a label-hidden or still-domain-blocked account stays hidden.
 *  Bounded rowid windows (short locks); runs in the background. */
async function recomputeDomainUnblock(db: TursoDB, domain: string): Promise<void> {
  const remaining = await getBlockedDomains(db);
  const suffix = "%." + domain;
  const max = await maxRowid(db);
  let cursor = 0;
  let restored = 0;
  while (cursor < max) {
    const lo = cursor;
    cursor += BACKFILL_WINDOW;
    const { results } = await db.prepare(
      `SELECT did, handle, labels FROM actors
       WHERE rowid > ?1 AND rowid <= ?2 AND (handle = ?3 OR handle LIKE ?4) COLLATE NOCASE`
    ).bind(lo, cursor, domain, suffix).all<{ did: string; handle: string; labels: string }>();
    if (!results || results.length === 0) continue;

    const overrides = await getOverrides(db, results.map((r) => r.did));
    const stmts = results.map((r) => {
      let labels: any[] = [];
      try { labels = JSON.parse(r.labels); } catch { labels = []; }
      const hidden = resolveHidden({
        labels,
        handle: r.handle,
        override: overrides.get(r.did) ?? null,
        domainBlocked: isDomainBlocked(r.handle || "", remaining),
      });
      return db.prepare(
        "UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2 AND hidden != ?1"
      ).bind(hidden, r.did);
    });
    const batchResults = await db.batch(stmts);
    restored += batchResults.filter((x) => x.meta.changes > 0).length;
  }
  console.log(JSON.stringify({ event: "domain_unblock_recompute", domain, restored }));
}

export async function handleDomainBlockDelete(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const domain = normalizeDomain(url.searchParams.get("domain") || "");
  if (!domain) return json({ error: "domain query param required" }, 400);

  await db.prepare("DELETE FROM blocked_domains WHERE domain = ?1").bind(domain).run();
  ctx.waitUntil(recomputeDomainUnblock(db, domain));

  return json({ domain, status: "unblocked; recomputing affected actors in background" });
}

export async function handleDomainBlockList(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const { results } = await db.prepare(
    "SELECT domain, reason, created_at FROM blocked_domains ORDER BY created_at DESC"
  ).all<{ domain: string; reason: string; created_at: number }>();

  return json({ blocked_domains: results || [] });
}

/* ─── profile enrichment ─────────────────────────────────────────────────
 * POST /admin/enrich-profiles — the canonical enrichment writer.
 * Accepts DIDs only; the worker fetches bsky `getProfiles`, applies
 * `extractProfileFields` + `resolveHidden`, and upserts. Body cannot lie
 * about counts/quality_score — the script is a pure orchestrator.
 * See docs/prioritized-enrich-plan.md.
 */

const ENRICH_MAX_DIDS = 100;
const ENRICH_BSKY_CHUNK = 25; // bsky's getProfiles per-call cap

type EnrichError = { did: string; reason: "bsky_404" | "bsky_error" | "rate_limited" | "timeout" | "db" };

export async function handleEnrichProfiles(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let body: { dids?: unknown };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const dids = body.dids;
  if (!Array.isArray(dids)) return json({ error: "body.dids must be an array of DIDs" }, 400);
  if (dids.length === 0) {
    return json({ ok: true, requested: 0, profiles_found: 0, written: 0, hidden: 0, errors: [] });
  }
  if (dids.length > ENRICH_MAX_DIDS) {
    return json({ error: `max ${ENRICH_MAX_DIDS} DIDs per request, got ${dids.length}` }, 400);
  }
  const bad = dids.find((d) => typeof d !== "string" || !(d as string).startsWith("did:"));
  if (bad !== undefined) return json({ error: `invalid DID shape: ${String(bad).slice(0, 80)}` }, 400);
  const didList = dids as string[];

  // batch-fetch the moderation inputs ONCE per request (caller pattern from
  // enrichment/cron). Overrides + blocked_domains scope the resolveHidden.
  const [overrides, blockedDomains] = await Promise.all([
    getOverrides(db, didList),
    getBlockedDomains(db),
  ]);

  // chunk to bsky's 25-per-call cap; run chunks concurrently.
  const chunks: string[][] = [];
  for (let i = 0; i < didList.length; i += ENRICH_BSKY_CHUNK) {
    chunks.push(didList.slice(i, i + ENRICH_BSKY_CHUNK));
  }
  const fetched = new Map<string, any>();
  const errors: EnrichError[] = [];

  await Promise.all(chunks.map(async (chunk) => {
    const qs = chunk.map((d) => `actors=${encodeURIComponent(d)}`).join("&");
    try {
      const res = await fetch(`${BSKY_GET_PROFILES_URL}?${qs}`);
      if (!res.ok) {
        const reason: EnrichError["reason"] = res.status === 429 ? "rate_limited" : "bsky_error";
        for (const d of chunk) errors.push({ did: d, reason });
        return;
      }
      const data: any = await res.json();
      for (const p of (data.profiles || [])) {
        if (p && p.did) fetched.set(p.did, p);
      }
    } catch {
      for (const d of chunk) errors.push({ did: d, reason: "timeout" });
    }
  }));

  // any DID we asked for that bsky didn't return is missing — could be a
  // takedown, deleted, or never-existed. Report so the script can decide.
  for (const did of didList) {
    if (!fetched.has(did) && !errors.find((e) => e.did === did)) {
      errors.push({ did, reason: "bsky_404" });
    }
  }

  // build upsert stmts via the canonical extractProfileFields path. PDS is
  // intentionally NOT in the SET — bsky's getProfiles doesn't return it, and
  // we'd rather preserve any existing value than zero it; scripts/plc-identity-sync.py is
  // the separate concern for filling PDS.
  const stmts: Stmt[] = [];
  let hiddenCount = 0;
  for (const [did, profile] of fetched) {
    const f = extractProfileFields(
      profile,
      overrides.get(did) ?? null,
      isDomainBlocked(profile.handle || "", blockedDomains),
    );
    if (f.hidden) hiddenCount++;
    stmts.push(
      db.prepare(
        `INSERT INTO actors (
           did, handle, display_name, avatar_url, labels, hidden, created_at,
           associated, followers_count, follows_count, posts_count, quality_score,
           profile_checked_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, unixepoch(), unixepoch())
         ON CONFLICT(did) DO UPDATE SET
           handle = excluded.handle,
           display_name = excluded.display_name,
           avatar_url = excluded.avatar_url,
           labels = excluded.labels,
           hidden = excluded.hidden,
           created_at = excluded.created_at,
           associated = excluded.associated,
           followers_count = COALESCE(NULLIF(excluded.followers_count, 0), actors.followers_count),
           follows_count = COALESCE(NULLIF(excluded.follows_count, 0), actors.follows_count),
           posts_count = COALESCE(NULLIF(excluded.posts_count, 0), actors.posts_count),
           quality_score = COALESCE(NULLIF(excluded.quality_score, 0), actors.quality_score),
           profile_checked_at = unixepoch(),
           updated_at = unixepoch()`
      ).bind(
        did, f.handle, f.displayName, f.avatarCid, f.labels, f.hidden,
        f.createdAt, f.associated,
        f.followersCount, f.followsCount, f.postsCount, f.qualityScore,
      )
    );
  }

  let written = 0;
  if (stmts.length > 0) {
    try {
      const results = await db.batch(stmts);
      written = results.filter((r) => (r.meta?.changes ?? 0) > 0).length;
    } catch {
      // wholesale batch failure: report a `db` error for every fetched DID
      // that didn't already have a per-DID error logged.
      for (const did of fetched.keys()) {
        if (!errors.find((e) => e.did === did)) errors.push({ did, reason: "db" });
      }
    }
  }

  return json({
    ok: true,
    requested: didList.length,
    profiles_found: fetched.size,
    written,
    hidden: hiddenCount,
    errors,
  });
}

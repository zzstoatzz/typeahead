import type { TursoDB } from "../db";
import type { Env, SlingshotResponse } from "../types";
import { SLINGSHOT_URL } from "../types";
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

  const cursor = await env.KV.get("jetstream_cursor");
  return json({ cursor: cursor ? Number(cursor) : null });
}

/** resolve a handle or DID via slingshot, then upsert into the active DB */
export async function handleRequestIndexing(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const url = new URL(request.url);
  const identifier =
    url.searchParams.get("handle") ||
    url.searchParams.get("did") ||
    "";

  if (!identifier) {
    return json({ error: "enter a handle or DID to request indexing." }, 400);
  }

  // resolve via slingshot
  const res = await fetch(
    `${SLINGSHOT_URL}?identifier=${encodeURIComponent(identifier)}`
  );
  if (!res.ok) {
    return json({ error: `could not resolve "${identifier}". check that it's a valid handle or DID.` }, 404);
  }

  const identity: SlingshotResponse = await res.json();

  // fetch profile from public API for display name + avatar + labels + metadata
  let f = {
    displayName: '', avatarCid: '', hidden: 0, labels: '[]', createdAt: '', associated: '{}',
    followersCount: 0, followsCount: 0, postsCount: 0, qualityScore: 0,
  };
  const override = await getOverride(db, identity.did);
  const domainBlocked = isDomainBlocked(identity.handle || "", await getBlockedDomains(db));
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
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

  return json({
    handle: identity.handle,
    did: identity.did,
    ...(f.hidden ? { hidden: true, reason: "hidden by moderation" } : { hidden: false }),
  });
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

export async function handleDomainBlockSet(
  request: Request,
  db: TursoDB,
  env: Env,
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

  await db.prepare(
    `INSERT INTO blocked_domains (domain, reason) VALUES (?1, ?2)
     ON CONFLICT(domain) DO UPDATE SET reason = ?2, created_at = unixepoch()`
  ).bind(domain, body.reason || "").run();

  // backfill: hide the apex + every subdomain, except DIDs a manual 'show'
  // override rescues. Only flip 0→1 so we don't churn updated_at (and the App B
  // sync) for accounts already hidden.
  const suffix = "%." + domain;
  const res = await db.prepare(
    `UPDATE actors SET hidden = 1, updated_at = unixepoch()
     WHERE (handle = ?1 OR handle LIKE ?2) COLLATE NOCASE
       AND hidden = 0
       AND did NOT IN (SELECT did FROM mod_overrides WHERE action = 'show')`
  ).bind(domain, suffix).run();

  return json({ domain, reason: body.reason || "", hidden_now: res.meta?.changes ?? 0 });
}

export async function handleDomainBlockDelete(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const domain = normalizeDomain(url.searchParams.get("domain") || "");
  if (!domain) return json({ error: "domain query param required" }, 400);

  await db.prepare("DELETE FROM blocked_domains WHERE domain = ?1").bind(domain).run();

  // recompute effective hidden for every actor that matched this domain, from
  // all remaining sources (labels + slur + OTHER domain blocks + override).
  // Paged by rowid to bound memory on large domains.
  const remaining = await getBlockedDomains(db);
  const suffix = "%." + domain;
  let restored = 0;
  let cursor = 0;
  while (true) {
    const { results } = await db.prepare(
      `SELECT rowid, did, handle, labels FROM actors
       WHERE (handle = ?1 OR handle LIKE ?2) COLLATE NOCASE AND rowid > ?3
       ORDER BY rowid ASC LIMIT 500`
    ).bind(domain, suffix, cursor).all<{ rowid: number; did: string; handle: string; labels: string }>();
    if (!results || results.length === 0) break;

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
    cursor = results[results.length - 1].rowid;
  }

  return json({ domain, recomputed: restored });
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

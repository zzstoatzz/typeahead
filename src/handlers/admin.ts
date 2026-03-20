import type { TursoDB } from "../db";
import type { Env, SlingshotResponse } from "../types";
import { SLINGSHOT_URL } from "../types";
import { clientIP, json, extractAvatarCid } from "../utils";
import { shouldHide } from "../moderation";
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

  const stmts = dids.map((did) =>
    db.prepare("DELETE FROM actors WHERE did = ?1").bind(did)
  );
  const batchResults = await db.batch(stmts);
  const actualDeletes = batchResults.reduce((s, r) => s + r.meta.changes, 0);

  if (actualDeletes > 0) {
    ctx.waitUntil(recordActorDelta(db, { actors: -actualDeletes }));
  }

  return json({ ok: true, deleted: dids.length });
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

  // fetch profile from public API for display name + avatar + labels
  let displayName = "";
  let avatarCid = "";
  let hidden = false;
  let labelsJson = "[]";
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
    if (profileRes.ok) {
      const profile: any = await profileRes.json();
      displayName = profile.displayName || "";
      avatarCid = extractAvatarCid(profile.avatar || "");
      hidden = shouldHide(profile.labels);
      labelsJson = JSON.stringify(profile.labels || []);
    }
  } catch {
    // profile enrichment is best-effort
  }

  await db.prepare(
    `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, labels, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, unixepoch())
     ON CONFLICT(did) DO UPDATE SET
       handle = ?2,
       display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
       avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
       hidden = ?5,
       labels = ?6,
       updated_at = unixepoch()`
  )
    .bind(identity.did, identity.handle, displayName, avatarCid, hidden ? 1 : 0, labelsJson)
    .run();

  return json({
    handle: identity.handle,
    did: identity.did,
    ...(hidden ? { hidden: true, reason: "hidden by moderation" } : { hidden: false }),
  });
}

import type { TursoDB } from "../db";
import type { ActorRow, Env } from "../types";
import { CORS_HEADERS } from "../types";
import { json, avatarUrl } from "../utils";

/** Batch profile lookup by DID or handle — compatible with app.bsky.actor.getProfiles response shape. */
export async function handleGetProfiles(
  request: Request,
  db: TursoDB,
  _env: Env,
): Promise<Response> {
  const url = new URL(request.url);
  const actors = url.searchParams.getAll("actors");

  if (actors.length === 0) {
    return json({ error: "InvalidRequest", message: "at least one 'actors' param required" }, 400);
  }
  if (actors.length > 25) {
    return json({ error: "InvalidRequest", message: "max 25 actors per request" }, 400);
  }

  // split into DIDs and handles
  const dids: string[] = [];
  const handles: string[] = [];
  for (const a of actors) {
    if (a.startsWith("did:")) dids.push(a);
    else handles.push(a);
  }

  // build query: lookup by DID and/or handle in one shot
  const conditions: string[] = [];
  const binds: string[] = [];
  let idx = 1;
  if (dids.length > 0) {
    const ph = dids.map(() => `?${idx++}`).join(", ");
    conditions.push(`did IN (${ph})`);
    binds.push(...dids);
  }
  if (handles.length > 0) {
    const ph = handles.map(() => `?${idx++}`).join(", ");
    conditions.push(`handle COLLATE NOCASE IN (${ph})`);
    binds.push(...handles);
  }

  const { results } = await db.prepare(
    `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
     FROM actors WHERE (${conditions.join(" OR ")}) AND hidden = 0`
  ).bind(...binds).all<ActorRow>();

  const profiles = (results || []).map((r) => ({
    did: r.did,
    handle: r.handle,
    ...(r.display_name ? { displayName: r.display_name } : {}),
    ...(r.avatar_url ? { avatar: avatarUrl(r.did, r.avatar_url, r.pds) } : {}),
    ...(r.associated && r.associated !== '{}' ? { associated: JSON.parse(r.associated) } : {}),
    labels: JSON.parse(r.labels || '[]'),
    ...(r.created_at ? { createdAt: r.created_at } : {}),
  }));

  return new Response(JSON.stringify({ profiles }), {
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

import type { TursoDB } from "../db";
import type { ProfileRow, Env } from "../types";
import { CORS_HEADERS } from "../types";
import { json, avatarUrl, avatarFallbackUrl, bannerUrl, type ProfileAssociated } from "../utils";

/** one actor in the response, keys in the order the wire contract serializes them.
 *  A profile-card subset of app.bsky.actor.defs#profileViewDetailed: everything a
 *  consumer needs to render a picked actor, nothing that needs a viewer or the graph. */
export interface ProfileView {
  did: string;
  handle: string;
  displayName?: string;
  description?: string;
  avatar?: string;
  avatarFallback?: string;
  banner?: string;
  /** bsky-sourced aggregates, present only once the enrich path has seen the actor */
  followersCount?: number;
  followsCount?: number;
  postsCount?: number;
  associated?: ProfileAssociated;
  labels?: unknown[];
  createdAt?: string;
  /** when our row was last written — how fresh this view is */
  indexedAt?: string;
}

export function profileView(r: ProfileRow): ProfileView {
  const view: ProfileView = { did: r.did, handle: r.handle };
  if (r.display_name) view.displayName = r.display_name;
  if (r.description) view.description = r.description;
  if (r.avatar_url) {
    view.avatar = avatarUrl(r.did, r.avatar_url, r.pds);
    const fallback = avatarFallbackUrl(r.did, r.avatar_url, r.pds);
    if (fallback) view.avatarFallback = fallback;
  }
  if (r.banner_url) view.banner = bannerUrl(r.did, r.banner_url);
  if (r.profile_checked_at > 0) {
    view.followersCount = r.followers_count;
    view.followsCount = r.follows_count;
    view.postsCount = r.posts_count;
  }
  if (r.associated && r.associated !== '{}') view.associated = JSON.parse(r.associated);
  view.labels = JSON.parse(r.labels || '[]');
  if (r.created_at) view.createdAt = r.created_at;
  if (r.updated_at > 0) view.indexedAt = new Date(r.updated_at * 1000).toISOString();
  return view;
}

/** edge cache TTL for getProfiles responses, in seconds. profiles change rarely
 *  (the enrichment cron refreshes them), so a few minutes of staleness is fine and
 *  collapses repeated lookups of the same actors from origin (Turso) reads to edge hits. */
export const PROFILES_CACHE_TTL = 300;

/** Build a stable Cache API key for a getProfiles request, independent of actor order,
 *  case, and duplicates — so `?actors=b&actors=A` and `?actors=a&actors=b` share one entry.
 *  DIDs are case-sensitive; handles are not, so only handles are lowercased. */
export function profilesCacheKey(actors: string[]): string {
  const norm = [...new Set(actors.map((a) => (a.startsWith("did:") ? a : a.toLowerCase())))].sort();
  return `https://typeahead.cache/xrpc/app.bsky.actor.getProfiles?actors=${norm.join(",")}`;
}

/** Batch profile lookup by DID or handle — compatible with app.bsky.actor.getProfiles response shape. */
export async function handleGetProfiles(
  request: Request,
  db: TursoDB,
  _env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const actors = url.searchParams.getAll("actors");

  if (actors.length === 0) {
    return json({ error: "InvalidRequest", message: "at least one 'actors' param required" }, 400);
  }
  if (actors.length > 25) {
    return json({ error: "InvalidRequest", message: "max 25 actors per request" }, 400);
  }

  // edge cache: a Worker-generated Response is never cached automatically, so we
  // explicitly look it up / store it via the Cache API keyed on the normalized actors.
  const cache = caches.default;
  const cacheKey = new Request(profilesCacheKey(actors), { method: "GET" });
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

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
    `SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds,
            description, banner_url, followers_count, follows_count, posts_count,
            profile_checked_at, updated_at
     FROM actors WHERE (${conditions.join(" OR ")}) AND hidden = 0`
  ).bind(...binds).all<ProfileRow>();

  const profiles = (results || []).map(profileView);

  const response = new Response(JSON.stringify({ profiles }), {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${PROFILES_CACHE_TTL}, s-maxage=${PROFILES_CACHE_TTL}`,
      ...CORS_HEADERS,
    },
  });

  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

/** app.bsky.actor.getProfile — the singular form most client code calls.
 *  Same lookup and cache as getProfiles; unwraps the one result. */
export async function handleGetProfile(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const actor = url.searchParams.get("actor");
  if (!actor) {
    return json({ error: "InvalidRequest", message: "'actor' param required" }, 400);
  }
  const batchUrl = new URL(url);
  batchUrl.search = `?actors=${encodeURIComponent(actor)}`;
  const batch = await handleGetProfiles(new Request(batchUrl, { method: "GET" }), db, env, ctx);
  if (!batch.ok) return batch;
  const { profiles }: { profiles: ProfileView[] } = await batch.json();
  const profile = profiles[0];
  if (!profile) {
    return json({ error: "InvalidRequest", message: "Profile not found" }, 400);
  }
  return new Response(JSON.stringify(profile), { headers: batch.headers });
}

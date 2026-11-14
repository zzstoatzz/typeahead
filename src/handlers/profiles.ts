import { filterIdentityResponse, isSearchExcludedHandle } from "../identity-policy";
import type { Env } from "../types";
import { CORS_HEADERS } from "../types";
import { json, avatarUrl, avatarFallbackUrl, bannerUrl, type ProfileAssociated } from "../utils";
import { slotActors, SlotReadError, type SlotActor, type SlotEnv } from "../slot-read";

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
  /** appview aggregates, present only once the store has read the actor's profile */
  followersCount?: number;
  followsCount?: number;
  postsCount?: number;
  associated?: ProfileAssociated;
  labels?: unknown[];
  createdAt?: string;
  /** when the store last changed the actor — how fresh this view is */
  indexedAt?: string;
}

/** The view of an actor as a store slot holds it. Counts are shown once the
 *  store has read the actor's appview profile, or when any of them is
 *  nonzero: nonzero counts only ever come from that read. */
export function slotProfileView(a: SlotActor): ProfileView {
  const view: ProfileView = { did: a.did, handle: a.handle };
  if (a.display_name) view.displayName = a.display_name;
  if (a.description) view.description = a.description;
  if (a.avatar_url) {
    view.avatar = avatarUrl(a.did, a.avatar_url, a.pds);
    const fallback = avatarFallbackUrl(a.did, a.avatar_url, a.pds);
    if (fallback) view.avatarFallback = fallback;
  }
  if (a.banner_url) view.banner = bannerUrl(a.did, a.banner_url);
  const countsKnown =
    a.profile_checked || a.followers_count > 0 || a.follows_count > 0 || a.posts_count > 0;
  if (countsKnown) {
    view.followersCount = a.followers_count;
    view.followsCount = a.follows_count;
    view.postsCount = a.posts_count;
  }
  if (Object.keys(a.associated).length > 0) view.associated = a.associated;
  view.labels = a.labels;
  if (a.created_at) view.createdAt = a.created_at;
  if (a.updated_at > 0) view.indexedAt = new Date(a.updated_at * 1000).toISOString();
  return view;
}

/** edge cache TTL for getProfiles responses, in seconds. Profiles change rarely,
 *  so a few minutes of staleness is fine and collapses repeated lookups of the
 *  same actors from slot reads to edge hits. */
export const PROFILES_CACHE_TTL = 300;

/** Build a stable Cache API key for a getProfiles request, independent of actor order,
 *  case, and duplicates — so `?actors=b&actors=A` and `?actors=a&actors=b` share one entry.
 *  DIDs are case-sensitive; handles are not, so only handles are lowercased. */
export function profilesCacheKey(actors: string[]): string {
  const norm = [...new Set(actors.map((a) => (a.startsWith("did:") ? a : a.toLowerCase())))].sort();
  return `https://typeahead.cache/xrpc/app.bsky.actor.getProfiles?actors=${norm.join(",")}`;
}

/** The actors named by DID or handle, from the routed store slot: visible
 *  actors only, in the order they were asked for. */
export async function slotProfiles(env: SlotEnv, actors: string[]): Promise<ProfileView[]> {
  return (await slotActors(env, actors))
    .filter((actor) => !actor.hidden && !isSearchExcludedHandle(actor.handle))
    .map(slotProfileView);
}

/** Batch profile lookup by DID or handle — compatible with app.bsky.actor.getProfiles response shape. */
export async function handleGetProfiles(
  request: Request,
  env: Env,
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
  if (cached) return filterIdentityResponse(cached, "profiles");

  let profiles: ProfileView[];
  try {
    profiles = await slotProfiles(env, actors);
  } catch (error) {
    console.log(JSON.stringify({
      event: "profile_slot_failed",
      status: error instanceof SlotReadError ? error.status : null,
      error: error instanceof Error ? error.message : String(error),
    }));
    return json({ error: "UpstreamFailure", message: "profile lookup is temporarily unavailable" }, 503);
  }

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
  const batch = await handleGetProfiles(new Request(batchUrl, { method: "GET" }), env, ctx);
  if (!batch.ok) return batch;
  const { profiles }: { profiles: ProfileView[] } = await batch.json();
  const profile = profiles[0];
  if (!profile) {
    return json({ error: "InvalidRequest", message: "Profile not found" }, 400);
  }
  return new Response(JSON.stringify(profile), { headers: batch.headers });
}

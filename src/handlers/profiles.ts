import { filterIdentityResponse, isSearchExcludedHandle } from "../identity-policy";
import type { SqlDb } from "../db";
import type { ProfileRow, Env } from "../types";
import { CORS_HEADERS } from "../types";
import { json, avatarUrl, avatarFallbackUrl, bannerUrl, type ProfileAssociated } from "../utils";
import { readMode, slotActors, SlotReadError, type SlotActor, type SlotEnv } from "../slot-read";

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

/** What a view is built from, whichever store answered. */
interface StoredProfile {
  did: string;
  handle: string;
  display_name: string;
  description: string;
  avatar_url: string;
  banner_url: string;
  pds: string;
  followers_count: number;
  follows_count: number;
  posts_count: number;
  /** the appview enrich path has read this actor at least once */
  profile_checked: boolean;
  /** absent when the actor has none */
  associated?: ProfileAssociated;
  labels: unknown[];
  created_at: string;
  updated_at: number;
}

function viewOf(r: StoredProfile): ProfileView {
  const view: ProfileView = { did: r.did, handle: r.handle };
  if (r.display_name) view.displayName = r.display_name;
  if (r.description) view.description = r.description;
  if (r.avatar_url) {
    view.avatar = avatarUrl(r.did, r.avatar_url, r.pds);
    const fallback = avatarFallbackUrl(r.did, r.avatar_url, r.pds);
    if (fallback) view.avatarFallback = fallback;
  }
  if (r.banner_url) view.banner = bannerUrl(r.did, r.banner_url);
  // In Turso an unchecked profile also means "re-check requested": /admin/ingest
  // resets profile_checked_at on every profile/identity event. Nonzero counts
  // only ever come from the appview enrich path, so they mark an actor that
  // path has already seen.
  const countsKnown =
    r.profile_checked || r.followers_count > 0 || r.follows_count > 0 || r.posts_count > 0;
  if (countsKnown) {
    view.followersCount = r.followers_count;
    view.followsCount = r.follows_count;
    view.postsCount = r.posts_count;
  }
  if (r.associated) view.associated = r.associated;
  view.labels = r.labels;
  if (r.created_at) view.createdAt = r.created_at;
  if (r.updated_at > 0) view.indexedAt = new Date(r.updated_at * 1000).toISOString();
  return view;
}

/** the view of a Turso `actors` row */
export function profileView(r: ProfileRow): ProfileView {
  return viewOf({
    ...r,
    profile_checked: r.profile_checked_at > 0,
    associated: r.associated && r.associated !== '{}' ? JSON.parse(r.associated) : undefined,
    labels: JSON.parse(r.labels || '[]'),
  });
}

/** the view of an actor as a store slot holds it */
export function slotProfileView(a: SlotActor): ProfileView {
  return viewOf({ ...a, associated: Object.keys(a.associated).length > 0 ? a.associated : undefined });
}

/** One actor that the two stores answer differently: absent from one, or the
 *  listed view fields differ. */
export interface ProfileDifference {
  did: string;
  only?: "turso" | "slot";
  fields?: string[];
}

/** The fields the two stores are expected to agree on once both have seen an
 *  actor's current profile. The three counts and `indexedAt` are left out:
 *  each store reads the appview on its own schedule and stamps its own
 *  writes. */
export const SETTLED_PROFILE_FIELDS = [
  "handle", "displayName", "description", "avatar", "avatarFallback", "banner", "associated", "labels", "createdAt",
] as const;

/** JSON with every object's keys in order. A slot stores `associated` and
 *  each label with sorted keys, as its search results already show them;
 *  Turso keeps the order the appview sent. */
function canonical(value: ProfileView[keyof ProfileView]): string {
  return JSON.stringify(value, (_key, inner) =>
    inner instanceof Object && !Array.isArray(inner)
      ? Object.fromEntries(Object.entries(inner).sort(([a], [b]) => (a < b ? -1 : 1)))
      : inner);
}

/** How `slot` differs from `turso`, actor by actor, whatever their order. */
export function profileDifferences(turso: ProfileView[], slot: ProfileView[]): ProfileDifference[] {
  const slotByDid = new Map(slot.map((p) => [p.did, p]));
  const differences: ProfileDifference[] = [];
  for (const expected of turso) {
    const actual = slotByDid.get(expected.did);
    slotByDid.delete(expected.did);
    if (!actual) {
      differences.push({ did: expected.did, only: "turso" });
      continue;
    }
    const wanted = new Map(Object.entries(expected));
    const found = new Map(Object.entries(actual));
    const fields = [...new Set([...wanted.keys(), ...found.keys()])]
      .filter((name) => canonical(wanted.get(name)) !== canonical(found.get(name)));
    if (fields.length > 0) differences.push({ did: expected.did, fields });
  }
  for (const did of slotByDid.keys()) differences.push({ did, only: "slot" });
  return differences;
}

const isSettledField = (name: string) => SETTLED_PROFILE_FIELDS.some((field) => field === name);

/** One log line per compared lookup: counts by kind and by field, and a few
 *  of the actors, so a parity gate can be read off the logs. */
export function compareSummary(asked: number, turso: ProfileView[], slot: ProfileView[]) {
  const differences = profileDifferences(turso, slot);
  const fields: Record<string, number> = {};
  for (const difference of differences) {
    for (const name of difference.fields ?? []) fields[name] = (fields[name] ?? 0) + 1;
  }
  return {
    event: "profile_compare",
    asked,
    turso: turso.length,
    slot: slot.length,
    only_turso: differences.filter((d) => d.only === "turso").length,
    only_slot: differences.filter((d) => d.only === "slot").length,
    settled_differ: differences.filter((d) => d.fields?.some(isSettledField)).length,
    fields,
    sample: differences.slice(0, 5),
  };
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

async function tursoProfiles(db: SqlDb, actors: string[]): Promise<ProfileView[]> {
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

  return (results || []).filter((row) => !isSearchExcludedHandle(row.handle)).map(profileView);
}

/** The same lookup against the routed store slot: visible actors only, in
 *  the order they were asked for. */
export async function slotProfiles(env: SlotEnv, actors: string[]): Promise<ProfileView[]> {
  return (await slotActors(env, actors))
    .filter((actor) => !actor.hidden && !isSearchExcludedHandle(actor.handle))
    .map(slotProfileView);
}

async function logComparison(env: SlotEnv, actors: string[], turso: ProfileView[]): Promise<void> {
  try {
    console.log(JSON.stringify(compareSummary(actors.length, turso, await slotProfiles(env, actors))));
  } catch (error) {
    console.log(JSON.stringify({
      event: "profile_compare_failed",
      status: error instanceof SlotReadError ? error.status : null,
      error: error instanceof Error ? error.message : String(error),
    }));
  }
}

/** Batch profile lookup by DID or handle — compatible with app.bsky.actor.getProfiles response shape. */
export async function handleGetProfiles(
  request: Request,
  db: SqlDb,
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

  const mode = readMode(env.PROFILE_MODE);
  let profiles: ProfileView[];
  if (mode === "slot") {
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
  } else {
    profiles = await tursoProfiles(db, actors);
    if (mode === "compare") ctx.waitUntil(logComparison(env, actors, profiles));
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
  db: SqlDb,
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

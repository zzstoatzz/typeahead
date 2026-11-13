import { INDEX_REQUEST_PAGE_MAX, controlDb, controlMeta, indexRequestPage, policySnapshot, type ControlMeta } from "../control-store";
import { validOverrideDid } from "../control-rules";
import type { SqlDb } from "../db";
import { tursoActorsOn } from "../turso-policy-mirror";
import type { Env } from "../types";
import { clientIP, extractAvatarCid } from "../utils";

/** What a store slot pulls: the policy snapshot and the indexing requests.
 *  Read-only, behind a secret used for nothing else. docs/slot-control.md. */

type SlotEnv = Pick<Env, "CONTROL_DB" | "SLOT_CONTROL_SECRET" | "ADMIN_SECRET" | "RATE_LIMITER_STRICT">;
type FillEnv = Pick<Env, "SLOT_CONTROL_SECRET" | "ADMIN_SECRET" | "RATE_LIMITER_STRICT" | "TURSO_ACTORS">;

const MIN_SECRET_BYTES = 32;
const INDEX_REQUEST_PAGE_DEFAULT = 100;
/** Never cached anywhere: the answer is per slot position and behind a secret. */
const NO_STORE = { "Cache-Control": "no-store" };

function answer<T extends object>(body: T, status: number, headers?: Record<string, string>): Response {
  return Response.json(body, { status, headers: { ...NO_STORE, ...headers } });
}

/** Compare by digest, so the time taken says nothing about where two secrets
 *  first differ or how long the expected one is. */
async function sameSecret(offered: string, expected: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(offered)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(a);
  const right = new Uint8Array(b);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i]! ^ right[i]!;
  return difference === 0;
}

/** The secret slots present, when this Worker has a usable one. One that is
 *  short, or that is the admin secret, is treated as absent: a slot must
 *  never hold a credential that can write. */
function slotSecret(env: Pick<SlotEnv, "SLOT_CONTROL_SECRET" | "ADMIN_SECRET">): string | null {
  const secret = env.SLOT_CONTROL_SECRET ?? "";
  if (new TextEncoder().encode(secret).length < MIN_SECRET_BYTES || secret === env.ADMIN_SECRET) return null;
  return secret;
}

async function refuse(request: Request, env: Pick<SlotEnv, "SLOT_CONTROL_SECRET" | "ADMIN_SECRET" | "RATE_LIMITER_STRICT">): Promise<Response | null> {
  const secret = slotSecret(env);
  if (secret === null) return answer({ error: "slot control is not configured" }, 503);
  const offered = request.headers.get("Authorization") ?? "";
  if (offered.startsWith("Bearer ") && await sameSecret(offered.slice("Bearer ".length), secret)) return null;
  const ip = clientIP(request);
  const { success } = await env.RATE_LIMITER_STRICT.limit({ key: `slot-auth:${ip}` });
  console.log(JSON.stringify({ event: "auth_failure", endpoint: new URL(request.url).pathname, ip }));
  return success ? answer({ error: "unauthorized" }, 401) : answer({ error: "rate limited" }, 429);
}

/** A non-negative integer query parameter; null when absent, NaN when it is
 *  anything else. */
function position(url: URL, name: string): number | null {
  const text = url.searchParams.get(name);
  if (text === null) return null;
  return /^\d{1,16}$/.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : Number.NaN;
}

const policyTag = (meta: ControlMeta) => `"policy-${meta.generation}-${meta.revision}"`;

/** `GET /slot/control/policy[?generation=<g>&after=<revision>]`: the whole
 *  current policy with its generation and revision, or 304 when the caller
 *  already holds that revision of this generation (both parameters, or
 *  `If-None-Match` with the ETag of an earlier answer). A revision without
 *  the generation it belongs to is never answered 304: the same number in
 *  a restored or recreated database names another policy. */
export async function handleSlotPolicy(request: Request, env: SlotEnv): Promise<Response> {
  const refused = await refuse(request, env);
  if (refused) return refused;
  const url = new URL(request.url);
  const after = position(url, "after");
  if (Number.isNaN(after)) return answer({ error: "after must be a revision" }, 400);
  const generation = position(url, "generation");
  if (Number.isNaN(generation)) return answer({ error: "generation must be a number" }, 400);

  const control = controlDb(env);
  const held = request.headers.get("If-None-Match");
  if ((after !== null && generation !== null) || held !== null) {
    const meta = await controlMeta(control);
    if ((after === meta.revision && generation === meta.generation) || held === policyTag(meta)) {
      return new Response(null, { status: 304, headers: { ...NO_STORE, ETag: policyTag(meta) } });
    }
  }
  const snapshot = await policySnapshot(control);
  return answer(snapshot, 200, { ETag: policyTag(snapshot) });
}

/** `GET /slot/control/index-requests?after=<seq>[&generation=<g>][&limit=<n>]`:
 *  requests with a sequence above `after`, oldest first, with the generation
 *  they belong to. `after` counts only when `generation` is this database's;
 *  otherwise the page starts at the beginning of the log. */
export async function handleSlotIndexRequests(request: Request, env: SlotEnv): Promise<Response> {
  const refused = await refuse(request, env);
  if (refused) return refused;
  const url = new URL(request.url);
  const after = position(url, "after") ?? 0;
  if (Number.isNaN(after)) return answer({ error: "after must be a sequence" }, 400);
  const limit = position(url, "limit") ?? INDEX_REQUEST_PAGE_DEFAULT;
  if (Number.isNaN(limit) || limit < 1 || limit > INDEX_REQUEST_PAGE_MAX) {
    return answer({ error: `limit must be 1 to ${INDEX_REQUEST_PAGE_MAX}` }, 400);
  }
  const generation = position(url, "generation");
  if (Number.isNaN(generation)) return answer({ error: "generation must be a number" }, 400);
  return answer(await indexRequestPage(controlDb(env), generation, after, limit), 200);
}

export const PROFILE_FILL_PAGE_MAX = 1000;
const PROFILE_FILL_PAGE_DEFAULT = 500;

/** One page of the scan, in primary-key order. Every row is read whether or
 *  not it qualifies, so the last one is where the next call starts. */
export const PROFILE_FILL_SQL = "SELECT did, description, banner_url FROM actors WHERE did > ?1 ORDER BY did LIMIT ?2";

interface ProfileFillRow { did: string; description: string; banner: string }

/** The CID in a stored `banner_url`: Turso rows hold a bare CID, a CDN URL
 *  or a getBlob URL, and a store's record holds the CID alone. A value no
 *  CID can be read from yields none. */
export function bannerCid(stored: string): string {
  try {
    return extractAvatarCid(stored);
  } catch {
    return "";
  }
}

/** `GET /slot/control/profile-fill?after=<did>[&limit=<n>]`: the descriptions
 *  and banners Turso holds, for the one-time pass that copies them into a
 *  store slot loaded from a bundle without them (docs/turso-retirement.md).
 *  A call reads the `limit` rows of `actors` after `after` in DID order and
 *  returns those with a description or a banner. `next` is the last DID
 *  read, to pass as the next `after`, and null once the table has ended; it
 *  moves on even when no row of the page qualified. `banner` is the blob's
 *  CID, the form a store's record holds, whatever shape Turso's
 *  `banner_url` has. */
export async function handleSlotProfileFill(request: Request, env: FillEnv, db: SqlDb): Promise<Response> {
  const refused = await refuse(request, env);
  if (refused) return refused;
  if (!tursoActorsOn(env)) return answer({ error: "Turso's actor tables are retired" }, 410);
  const url = new URL(request.url);
  const after = url.searchParams.get("after") ?? "";
  if (after !== "" && !validOverrideDid(after)) return answer({ error: "after must be a DID" }, 400);
  const limit = position(url, "limit") ?? PROFILE_FILL_PAGE_DEFAULT;
  if (Number.isNaN(limit) || limit < 1 || limit > PROFILE_FILL_PAGE_MAX) {
    return answer({ error: `limit must be 1 to ${PROFILE_FILL_PAGE_MAX}` }, 400);
  }

  const { results } = await db.prepare(PROFILE_FILL_SQL).bind(after, limit)
    .all<{ did: string; description: string | null; banner_url: string | null }>();
  const rows: ProfileFillRow[] = [];
  for (const row of results) {
    const description = row.description ?? "";
    const banner = bannerCid(row.banner_url ?? "");
    if (description !== "" || banner !== "") rows.push({ did: row.did, description, banner });
  }
  const last = results.at(-1);
  return answer({ rows, next: results.length === limit && last !== undefined ? last.did : null }, 200);
}

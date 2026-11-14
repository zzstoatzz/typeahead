import { identitySchema, verifyIdentityHandle } from "../verify-identity";
import type { Stmt, SqlDb } from "../db";
import type { Env } from "../types";
import { SLINGSHOT_URL, BSKY_GET_PROFILES_URL, MICROCOSM_UA } from "../types";
import { clientIP, json, extractProfileFields } from "../utils";
import { getOverride, getOverrides, isDomainBlocked, getBlockedDomains } from "../moderation";
import { blockDomain, clearOverride, controlDb, listBlockedDomains, listOverrides, recordIndexRequest, setOverride, unblockDomain } from "../control-store";
import { noteControlFailure } from "../control-health";
import { MAX_DID_BYTES, validBlockedDomain, validOverrideDid } from "../control-rules";
import { slotActors, SlotReadError } from "../slot-read";
import { resolveHidden } from "../moderation";
import { recordActorDelta } from "../metrics";
import { recordStats } from "../stats-store";

const unixNow = () => Math.floor(Date.now() / 1000);

export async function handleDelete(
  request: Request,
  db: SqlDb,
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
    let batchResults: Awaited<ReturnType<typeof db.batch>>;
    try {
      batchResults = await db.batch(stmts);
    } catch (e: any) {
      console.log(JSON.stringify({ event: "delete_error", error: e?.message, count: dids.length, deleted: actualDeletes }));
      return json({ error: e?.message || "db batch failed", deleted: actualDeletes }, 500);
    }
    actualDeletes += batchResults
      .filter((_, j) => j % 2 === 0)
      .reduce((s, r) => s + r.meta.changes, 0);
  }

  console.log(JSON.stringify({
    event: "admin_delete", requested: dids.length, deleted: actualDeletes,
    chunks: Math.ceil(dids.length / CHUNK), ms: Date.now() - t0,
  }));

  if (actualDeletes > 0) {
    recordStats(ctx, env, db, (store, now) => recordActorDelta(store, { actors: -actualDeletes }, now));
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

/** A valid PDS that never announced itself is invisible to the relay, so its
 *  accounts never reach the firehose — for us or ANY consumer (hey.pds.zat.dev,
 *  2026-08-15: PLC knew the account, no relay had ever crawled the host, and
 *  the weekly PLC sync only updates existing rows so nothing would ever have
 *  discovered it). Whoever lands on request-indexing from a self-hosted PDS is
 *  exactly that case, so announce their host to the relay while we index them.
 *  KV-memoized per host (7d) so the relay probe doesn't run per request. */
async function announceHostToRelay(pdsUrl: string, env: Env): Promise<void> {
  try {
    const hostname = new URL(pdsUrl).hostname;
    if (hostname.endsWith(".bsky.network")) return; // mothership: always crawled
    if (await env.KV.get(`relay_crawl:${hostname}`)) return;
    const status = await fetch(
      `https://bsky.network/xrpc/com.atproto.sync.getHostStatus?hostname=${encodeURIComponent(hostname)}`
    );
    if (!status.ok) {
      await fetch("https://bsky.network/xrpc/com.atproto.sync.requestCrawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hostname }),
      });
      console.log(JSON.stringify({ event: "relay_request_crawl", hostname }));
    }
    await env.KV.put(`relay_crawl:${hostname}`, "1", { expirationTtl: 7 * 86400 });
  } catch {
    // best-effort — the relay announce is a favor, never a dependency
  }
}

export async function handleRequestIndexing(
  request: Request,
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
    `${SLINGSHOT_URL}?identifier=${encodeURIComponent(identifier)}`,
    { headers: { "User-Agent": MICROCOSM_UA } }
  );
  if (!res.ok) {
    return json({ error: `could not resolve "${identifier}". check that it's a valid handle or DID.` }, 404);
  }

  const parsedIdentity = identitySchema.safeParse(await res.json());
  if (!parsedIdentity.success || !await verifyIdentityHandle(parsedIdentity.data)) {
    return json({ error: "handle ownership could not be verified" }, 422);
  }
  const identity = parsedIdentity.data;
  lap("slingshot");

  // the request itself: store slots pull it and index the account from their
  // own sources. Everything after this serves the response.
  //
  // A control database that is down or unmigrated must not turn a visitor's
  // request into an error: the failure is logged and kept for
  // /health/control, and the moderation answer below comes from the
  // account's labels alone. What is lost is the request reaching the store
  // slots.
  let control: SqlDb | null = null;
  try {
    const opened = controlDb(env);
    await recordIndexRequest(opened, identity.did, unixNow());
    control = opened;
  } catch (error) {
    noteControlFailure(ctx, env, "index_request", error instanceof Error ? error.message : String(error));
  }
  lap("control_write");

  const policyIn = async (store: SqlDb) => {
    const found = await getOverride(store, identity.did);
    lap("override");
    return { override: found, blockedDomains: await getBlockedDomains(store) };
  };
  let policy: Awaited<ReturnType<typeof policyIn>> = { override: null, blockedDomains: new Set() };
  if (control) {
    try {
      policy = await policyIn(control);
    } catch (error) {
      noteControlFailure(ctx, env, "index_request_policy", error instanceof Error ? error.message : String(error));
    }
  }
  const { override, blockedDomains } = policy;
  const domainBlocked = isDomainBlocked(identity.handle || "", blockedDomains);
  lap("blocked_domains");
  // the account's labels, as the public appview reports them: with the
  // policy above they decide the `hidden` this response states
  let hidden = 0;
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
    lap("bsky_profile");
    if (profileRes.ok) hidden = extractProfileFields(await profileRes.json(), override, domainBlocked).hidden;
  } catch {
    // best-effort: without the profile the account is reported visible
  }

  // fire-and-forget: fix the upstream invisibility too (see announceHostToRelay)
  if (identity.pds) ctx.waitUntil(announceHostToRelay(identity.pds, env));

  const timing = Object.entries(t).map(([k, v]) => `${k};dur=${v}`).join(", ");
  return json({
    handle: identity.handle,
    did: identity.did,
    ...(hidden ? { hidden: true, reason: "hidden by moderation" } : { hidden: false }),
  }, 200, { "Server-Timing": timing });
}

/** An operator's policy change with the control database out of reach: say
 *  so, and change nothing. An operator is there to see it and try again. */
async function controlWrite(env: Env, what: string, write: (control: SqlDb) => Promise<void>): Promise<Response | null> {
  try {
    await write(controlDb(env));
    return null;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    console.log(JSON.stringify({ event: "control_write_failed", what, error: detail.slice(0, 300) }));
    return json({ error: "the control database is unavailable; nothing was changed", detail: detail.slice(0, 300) }, 503);
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
  // a subject a slot would refuse would be served with the rest of the
  // policy, and every slot would refuse the whole snapshot from then on
  if (did !== String(did) || !validOverrideDid(did)) {
    return json({ error: `did must be a DID: "did:" and printable ASCII without spaces, at most ${MAX_DID_BYTES} bytes` }, 400);
  }

  const failed = await controlWrite(env, "override_set", (control) => setOverride(control, did, action, reason || '', unixNow()));
  if (failed) return failed;

  return json({ did, action, reason: reason || '', hidden: action === 'show' ? 0 : 1 });
}

export async function handleModOverrideDelete(
  request: Request,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const did = url.searchParams.get("did") || "";
  if (!did) return json({ error: "did query param required" }, 400);

  const failed = await controlWrite(env, "override_clear", (control) => clearOverride(control, did));
  if (failed) return failed;

  try {
    return json({ did, hidden: await hiddenWithoutOverride(env, did) });
  } catch (error) {
    if (!(error instanceof SlotReadError)) throw error;
    return json({ error: "the override was cleared; the account's visibility could not be read from the slot", did }, 503);
  }
}

/** The account's `hidden` once it has no override, by the rules a slot
 *  applies: its stored labels and handle, and the blocked domains. 0 for an
 *  account the routed slot does not hold. */
async function hiddenWithoutOverride(env: Env, did: string): Promise<0 | 1> {
  const [actor] = await slotActors(env, [did]);
  if (!actor) return 0;
  const blockedDomains = await getBlockedDomains(controlDb(env));
  return resolveHidden({
    labels: actor.labels,
    handle: actor.handle,
    override: null,
    domainBlocked: isDomainBlocked(actor.handle, blockedDomains),
  });
}

export async function handleModOverrideList(
  request: Request,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let overrides: Awaited<ReturnType<typeof listOverrides>> = [];
  const failed = await controlWrite(env, "override_list", async (control) => { overrides = await listOverrides(control); });
  return failed ?? json({ overrides });
}

/** normalize an input domain: lowercase, trim, strip a leading "*." / "." */
function normalizeDomain(raw: string): string {
  return (raw || "").trim().toLowerCase().replace(/^\*?\.?/, "");
}

export async function handleDomainBlockSet(
  request: Request,
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
  if (!domain || !domain.includes(".") || /\s/.test(domain) || !validBlockedDomain(domain)) {
    return json({ error: "valid domain required (e.g. dadavidtseng.com)" }, 400);
  }

  const failed = await controlWrite(env, "domain_block", (control) => blockDomain(control, domain, body.reason || "", unixNow()));
  if (failed) return failed;

  return json({ domain, reason: body.reason || "", status: "blocked; backfilling existing matches in background" });
}

export async function handleDomainBlockDelete(
  request: Request,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const domain = normalizeDomain(url.searchParams.get("domain") || "");
  if (!domain) return json({ error: "domain query param required" }, 400);

  const failed = await controlWrite(env, "domain_unblock", (control) => unblockDomain(control, domain));
  if (failed) return failed;

  return json({ domain, status: "unblocked; recomputing affected actors in background" });
}

export async function handleDomainBlockList(
  request: Request,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let blocked_domains: Awaited<ReturnType<typeof listBlockedDomains>> = [];
  const failed = await controlWrite(env, "domain_list", async (control) => { blocked_domains = await listBlockedDomains(control); });
  return failed ?? json({ blocked_domains });
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
  db: SqlDb,
  env: Env,
): Promise<Response> {
  const denied = authCheck(request, env);
  if (denied) return denied;

  let raw: { dids?: unknown };
  try {
    raw = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const parsed = parseEnrichBody(raw);
  if ("error" in parsed) return json({ error: parsed.error }, 400);
  if (parsed.body.dids.length === 0) {
    return json({ ok: true, requested: 0, profiles_found: 0, written: 0, hidden: 0, errors: [] });
  }

  return json(await enrichProfileDids(db, env, parsed.body.dids));
}

interface EnrichBody {
  dids: string[];
}

function parseEnrichBody(raw: { dids?: unknown }): { body: EnrichBody } | { error: string } {
  const dids = raw.dids;
  if (!Array.isArray(dids)) return { error: "body.dids must be an array of DIDs" };
  if (dids.length > ENRICH_MAX_DIDS) {
    return { error: `max ${ENRICH_MAX_DIDS} DIDs per request, got ${dids.length}` };
  }
  const valid = dids.filter((d): d is string => d === String(d) && d.startsWith("did:"));
  if (valid.length !== dids.length) {
    const bad = dids.find((d) => !valid.includes(d));
    return { error: `invalid DID shape: ${String(bad).slice(0, 80)}` };
  }
  return { body: { dids: valid } };
}

/** The canonical enrichment core, callable in-worker (reputation's on-view
 *  enrichment) as well as via the authed HTTP surface above. One DID list in,
 *  getProfiles + extractProfileFields + resolveHidden + guarded upsert out. */
export async function enrichProfileDids(
  db: SqlDb,
  env: Env,
  didList: string[],
): Promise<{ ok: true; requested: number; profiles_found: number; written: number; hidden: number; errors: EnrichError[] }> {

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
  // preserve existing hosting identity when refreshing profile fields.
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
           description, banner_url, profile_checked_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, unixepoch(), unixepoch())
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
           description = COALESCE(NULLIF(excluded.description, ''), actors.description),
           banner_url = COALESCE(NULLIF(excluded.banner_url, ''), actors.banner_url),
           profile_checked_at = unixepoch(),
           updated_at = unixepoch()`
      ).bind(
        did, f.handle, f.displayName, f.avatarCid, f.labels, f.hidden,
        f.createdAt, f.associated,
        f.followersCount, f.followsCount, f.postsCount, f.qualityScore,
        f.description, f.bannerCid,
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

  return {
    ok: true,
    requested: didList.length,
    profiles_found: fetched.size,
    written,
    hidden: hiddenCount,
    errors,
  };
}

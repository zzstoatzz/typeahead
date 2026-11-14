import { identitySchema, verifyIdentityHandle } from "../verify-identity";
import type { SqlDb } from "../db";
import type { Env } from "../types";
import { SLINGSHOT_URL, MICROCOSM_UA } from "../types";
import { clientIP, json, extractProfileFields } from "../utils";
import { isDomainBlocked } from "../moderation";
import { blockDomain, clearOverride, controlDb, getBlockedDomains, getOverride, listBlockedDomains, listOverrides, recordIndexRequest, setOverride, unblockDomain } from "../control-store";
import { noteControlFailure } from "../control-health";
import { MAX_DID_BYTES, validBlockedDomain, validOverrideDid } from "../control-rules";
import { slotActors, SlotReadError } from "../slot-read";
import { resolveHidden } from "../moderation";

const unixNow = () => Math.floor(Date.now() / 1000);

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

  return json({ domain, reason: body.reason || "", status: "blocked; slots apply it on their next policy pull" });
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

  return json({ domain, status: "unblocked; slots apply it on their next policy pull" });
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

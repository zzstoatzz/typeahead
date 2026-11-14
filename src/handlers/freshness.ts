import type { Env } from "../types";
import { json } from "../utils";

/** How stale the serving path is, measured end to end.
 *
 *  A liveness check on one component is not enough: on 2026-07-28 every such
 *  check stayed green while search served 48-hour-old data for two days. So
 *  this measures how far the serving slot's index is behind its store, and
 *  whether a search for a known handle finds it, from outside the slot.
 */

// A slot stops answering searches once its index is two minutes behind its
// store, so past ten minutes something is wrong and past thirty it is stale.
const WARN_LAG_S = 10 * 60;
const STALE_LAG_S = 30 * 60;
const PROBE_HANDLE = "zzstoatzz.io";

/** what the serving slot answers on /health, /index/status and /search */
type HealthBody = { ready?: boolean; sync_lag_s?: number };
type IndexStatusBody = { build_id?: string };
type SearchBody = { actors?: { handle?: string }[]; results?: { handle?: string }[] };

export type Freshness = {
  ok: boolean;
  stale: boolean;
  /** seconds the serving slot's index is behind its store */
  index_lag_s: number | null;
  build_id: string | null;
  probe_ok: boolean | null;
  reasons: string[];
};

export async function measureFreshness(env: Env): Promise<Freshness> {
  const base = env.SEARCH_BACKEND_URL;
  const out: Freshness = {
    ok: true, stale: false,
    index_lag_s: null, build_id: null, probe_ok: null,
    reasons: [],
  };
  if (!base) {
    out.reasons.push("no SEARCH_BACKEND_URL configured");
    return out;
  }

  const get = async <T>(path: string): Promise<T | null> => {
    try {
      const r = await fetch(base + path, { signal: AbortSignal.timeout(5000) });
      return r.ok ? await r.json<T>() : null;
    } catch {
      return null;
    }
  };

  const [health, index] = await Promise.all([
    get<HealthBody>("/health"),
    get<IndexStatusBody>("/index/status"),
  ]);

  if (!health) {
    out.ok = false;
    out.reasons.push("search service /health unreachable");
  } else {
    const lag = health.sync_lag_s;
    out.index_lag_s = lag !== undefined && Number.isFinite(lag) ? lag : null;
    if (health.ready === false) {
      out.ok = false;
      out.reasons.push("search service not ready");
    }
    if (out.index_lag_s !== null && out.index_lag_s > STALE_LAG_S) {
      out.stale = true;
      out.reasons.push(`index ${out.index_lag_s}s behind its store (>${STALE_LAG_S}s)`);
    } else if (out.index_lag_s !== null && out.index_lag_s > WARN_LAG_S) {
      out.reasons.push(`index ${out.index_lag_s}s behind its store`);
    }
  }

  out.build_id = index?.build_id ?? null;

  // End-to-end probe: a search through the real serving path. The handle is
  // the one smoke.py asserts must rank first for its own query, so if it ever
  // stops being found that is a real finding either way.
  const probe = await get<SearchBody>(`/search?q=${encodeURIComponent(PROBE_HANDLE)}&limit=5`);
  if (probe) {
    const actors = probe.actors ?? probe.results ?? [];
    out.probe_ok = actors.some((a) => a.handle === PROBE_HANDLE);
    if (!out.probe_ok) {
      out.ok = false;
      out.reasons.push(`serving path cannot find ${PROBE_HANDLE}`);
    }
  }

  if (!out.ok) out.stale = true;
  return out;
}

/** The uptime-monitor surface. Public, no auth, no dependencies of its own.
 *
 *  200 = the serving path is current. 503 = it is not, with `reasons` saying
 *  why. That is the whole contract, so any monitor that understands HTTP status
 *  can watch it.
 *
 *  no-store: a monitor must see the current state, never an edge-cached one.
 */
export async function handleFreshness(env: Env): Promise<Response> {
  const f = await measureFreshness(env);
  return json(f, f.stale ? 503 : 200, {
    "Cache-Control": "no-store",
    // some monitors alert on a body match rather than a status code
    "X-Freshness": f.stale ? "stale" : "fresh",
  });
}

/** Post to the webhook only when the state CHANGES.
 *
 *  Hourly "still broken" messages train you to ignore the channel, and a
 *  channel you ignore is the same as no alerting — which is what we had.
 *  Mirrors Logfire's has_matches_changed semantics, but depends on nothing
 *  except the webhook itself.
 */
export async function notifyFreshnessChange(env: Env, f: Freshness): Promise<void> {
  if (!env.ALERT_WEBHOOK_URL) return;

  let previous: boolean | null = null;
  try {
    const prev = await env.KV.get<{ stale?: boolean }>("freshness", "json");
    if (prev?.stale === true || prev?.stale === false) previous = prev.stale;
  } catch { /* no prior state: a change only if stale */ }

  if (previous === f.stale) return;                 // no change
  if (previous === null && !f.stale) return;        // first run and healthy

  const lag = f.index_lag_s === null ? "?" : `${Math.round(f.index_lag_s / 60)}m`;
  const content = f.stale
    ? [
        "**typeahead: serving is stale**",
        "```", f.reasons.join("\n") || "unknown", "```",
        `index lag ${lag} · probe ${f.probe_ok ? "ok" : "FAILING"}`,
        "https://typeahead.waow.tech/health/freshness",
      ].join("\n")
    : `**typeahead: serving is fresh again** (index lag ${lag})`;

  await postWebhook(env, content);
}

/** Returns what actually happened, so a failure is observable rather than
 *  swallowed. Discord answers 204 on success and puts the reason in the body
 *  on rejection — both worth surfacing. */
export async function postWebhook(env: Env, content: string): Promise<{ ok: boolean; status: number | null; body: string }> {
  if (!env.ALERT_WEBHOOK_URL) return { ok: false, status: null, body: "ALERT_WEBHOOK_URL not set" };
  // a secret pasted with a trailing newline produces an opaque fetch failure
  const url = env.ALERT_WEBHOOK_URL.trim();
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal: AbortSignal.timeout(5000),
    });
    const body = await r.text().catch(() => "");
    const out = { ok: r.ok, status: r.status, body: body.slice(0, 300) };
    console.log(JSON.stringify({ event: "freshness_notify", ...out }));
    return out;
  } catch (e: any) {
    const out = { ok: false, status: null, body: String(e?.message ?? e).slice(0, 300) };
    console.log(JSON.stringify({ event: "freshness_notify_failed", ...out }));
    return out;
  }
}

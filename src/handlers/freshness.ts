import type { Env } from "../types";
import { json } from "../utils";

/** How stale the serving path is, measured end to end.
 *
 *  Every existing check is a LIVENESS check on one component: the ingester
 *  heartbeat, the promote poller's build id, actor counts. On 2026-07-28 all of
 *  them stayed green while the replica sync silently stopped, and search served
 *  48-hour-old data for two days. `sync_lag_s` was in the search service's
 *  /health the entire time, reporting 175469, and nothing read it.
 *
 *  So this measures the number that actually matters — how long ago the data
 *  behind /search was current — and does it from outside, so it stays true no
 *  matter which component is the one that broke.
 */

// The replica syncs every 5 min and the edge caches for 60s, so anything under
// ~10 min is normal operation. Past 30 min something is wrong, not just slow.
const WARN_LAG_S = 10 * 60;
const STALE_LAG_S = 30 * 60;
// the snapshot rebuild runs every 3 days; past 4 the job itself is failing
const SNAPSHOT_STALE_S = 4 * 24 * 3600;
const PROBE_HANDLE = "zzstoatzz.io";

export type Freshness = {
  ok: boolean;
  stale: boolean;
  replica_lag_s: number | null;
  snapshot_age_s: number | null;
  build_id: string | null;
  probe_ok: boolean | null;
  reasons: string[];
};

/** build ids look like "b1785229391-21fc" — the epoch seconds of the build */
function snapshotAge(buildId: string | null): number | null {
  const m = buildId?.match(/^b(\d{10})-/);
  if (!m) return null;
  return Math.floor(Date.now() / 1000) - parseInt(m[1]!, 10);
}

export async function measureFreshness(env: Env): Promise<Freshness> {
  const base = env.SEARCH_BACKEND_URL;
  const out: Freshness = {
    ok: true, stale: false,
    replica_lag_s: null, snapshot_age_s: null, build_id: null, probe_ok: null,
    reasons: [],
  };
  if (!base) {
    out.reasons.push("no SEARCH_BACKEND_URL configured");
    return out;
  }

  const get = async (path: string): Promise<any | null> => {
    try {
      const r = await fetch(base + path, { signal: AbortSignal.timeout(5000) });
      return r.ok ? await r.json() : null;
    } catch {
      return null;
    }
  };

  const [health, index] = await Promise.all([get("/health"), get("/index/status")]);

  if (!health) {
    out.ok = false;
    out.reasons.push("search service /health unreachable");
  } else {
    out.replica_lag_s = typeof health.sync_lag_s === "number" ? health.sync_lag_s : null;
    if (health.ready === false) {
      out.ok = false;
      out.reasons.push("search service not ready");
    }
    if (out.replica_lag_s !== null && out.replica_lag_s > STALE_LAG_S) {
      out.stale = true;
      out.reasons.push(`replica ${out.replica_lag_s}s behind turso (>${STALE_LAG_S}s)`);
    } else if (out.replica_lag_s !== null && out.replica_lag_s > WARN_LAG_S) {
      out.reasons.push(`replica ${out.replica_lag_s}s behind turso`);
    }
  }

  out.build_id = index?.build_id ?? null;
  out.snapshot_age_s = snapshotAge(out.build_id);
  if (out.snapshot_age_s !== null && out.snapshot_age_s > SNAPSHOT_STALE_S) {
    out.stale = true;
    out.reasons.push(`snapshot ${Math.round(out.snapshot_age_s / 3600)}h old`);
  }

  // End-to-end probe: exercise the real serving path — replica, snapshot,
  // overlay and ranking — rather than trusting a component's self-report.
  //
  // The handle must be one we are certain is in the corpus, or the canary cries
  // wolf. The first version probed the service's own account and reported a
  // failure on its first run, because that account has never been indexed. This
  // one is the same handle smoke.py asserts must rank first for its own query,
  // so if it ever stops being present, that is a real finding either way.
  const probe = await get(`/search?q=${encodeURIComponent(PROBE_HANDLE)}&limit=5`);
  if (probe) {
    const actors = probe.actors ?? probe.results ?? [];
    out.probe_ok = actors.some((a: any) => a.handle === PROBE_HANDLE);
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
 *  can watch it — deliberately not routed through Logfire, because the 48-hour
 *  wedge on 2026-07-28 was invisible partly because this service exported no
 *  telemetry at all.
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

  let wasStale: boolean | null = null;
  try {
    const prev = await env.KV.get("freshness", "json") as { stale?: boolean } | null;
    wasStale = prev && typeof prev.stale === "boolean" ? prev.stale : null;
  } catch { /* no prior state — treat as a change only if stale */ }

  if (wasStale === f.stale) return;                 // no change
  if (wasStale === null && !f.stale) return;        // first run and healthy

  const lag = f.replica_lag_s === null ? "?" : `${Math.round(f.replica_lag_s / 60)}m`;
  const snap = f.snapshot_age_s === null ? "?" : `${Math.round(f.snapshot_age_s / 3600)}h`;
  const content = f.stale
    ? [
        "**typeahead: serving is stale**",
        "```",
        f.reasons.join("\n") || "unknown",
        "```",
        `replica lag ${lag} · snapshot ${snap} · probe ${f.probe_ok ? "ok" : "FAILING"}`,
        "https://typeahead.waow.tech/health/freshness",
      ].join("\n")
    : `**typeahead: serving is fresh again** — replica lag ${lag}, snapshot ${snap}`;

  try {
    await fetch(env.ALERT_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal: AbortSignal.timeout(5000),
    });
  } catch (e: any) {
    console.log(JSON.stringify({ event: "freshness_notify_failed", error: e?.message }));
  }
}

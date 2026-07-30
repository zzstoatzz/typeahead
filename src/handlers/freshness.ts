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

/** public, cheap, cacheable — so an uptime check can watch it without a token */
export async function handleFreshness(env: Env): Promise<Response> {
  const f = await measureFreshness(env);
  return json(f, f.stale ? 503 : 200);
}

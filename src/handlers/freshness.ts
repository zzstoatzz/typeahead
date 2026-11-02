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
// Managed refresh runs at most 48h apart; keep the existing 4-day stale threshold.
const SNAPSHOT_STALE_S = 4 * 24 * 3600;
const PROBE_HANDLE = "zzstoatzz.io";
// Tantivy checkpoint coverage window: a completed full source round (or the
// checkpoint cut) is current for 24h. Slots keep serving past it, so this
// report is what alerts when a scheduled refresh is missed.
const CHECKPOINT_SOURCE = "sqlite-committed-prefix";
const COVERAGE_WINDOW_S = 24 * 3600;
// Refreshes run every 12 h and a checkpoint is at most ~13.7 h old when its
// replacement is promoted, so 15 h means a scheduled refresh was missed while
// there is still time to run another before the slot goes stale.
const COVERAGE_OVERDUE_S = 15 * 3600;

/** what the search service answers on /health, /index/status and /search */
type HealthBody = { ready?: boolean; sync_lag_s?: number };
export type IndexStatusBody = {
  build_id?: string;
  source_manifest?: {
    source_kind?: string;
    source_bootstrap?: { checkpoint_receipt?: { source_clock_before?: number } };
  };
  reconciliation?: { last_completed_at?: number | null };
};
type SearchBody = { actors?: { handle?: string }[]; results?: { handle?: string }[] };

export type Freshness = {
  ok: boolean;
  stale: boolean;
  replica_lag_s: number | null;
  snapshot_age_s: number | null;
  build_id: string | null;
  coverage_age_s: number | null;
  coverage_deadline: number | null;
  refresh_overdue: boolean;
  probe_ok: boolean | null;
  reasons: string[];
};

export type FreshnessLevel = "fresh" | "overdue" | "stale";

export function freshnessLevel(f: Pick<Freshness, "stale" | "refresh_overdue">): FreshnessLevel {
  if (f.stale) return "stale";
  return f.refresh_overdue ? "overdue" : "fresh";
}

/** Mirrors the installer's checkpoint coverage anchor: the later of the
 *  checkpoint cut and the last completed full source round. Null for a
 *  backend that does not serve a checkpoint-sourced generation. */
export function checkpointCoverageAnchor(index: IndexStatusBody | null): number | null {
  const source = index?.source_manifest;
  if (source?.source_kind !== CHECKPOINT_SOURCE) return null;
  const cut = source.source_bootstrap?.checkpoint_receipt?.source_clock_before;
  if (cut === undefined || !Number.isFinite(cut)) return null;
  const round = index?.reconciliation?.last_completed_at;
  return round === undefined || round === null || !Number.isFinite(round) ? cut : Math.max(cut, round);
}

/** Legacy and compact build IDs retain the source build epoch. */
function snapshotAge(buildId: string | null): number | null {
  const m = buildId?.match(/^(?:compact-)?b(\d{10})-/);
  if (!m) return null;
  return Math.floor(Date.now() / 1000) - parseInt(m[1]!, 10);
}

export async function measureFreshness(env: Env): Promise<Freshness> {
  const base = env.SEARCH_BACKEND_URL;
  const out: Freshness = {
    ok: true, stale: false,
    replica_lag_s: null, snapshot_age_s: null, build_id: null,
    coverage_age_s: null, coverage_deadline: null, refresh_overdue: false, probe_ok: null,
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
    out.replica_lag_s = lag !== undefined && Number.isFinite(lag) ? lag : null;
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

  const anchor = checkpointCoverageAnchor(index);
  if (anchor !== null) {
    out.coverage_deadline = anchor + COVERAGE_WINDOW_S;
    out.coverage_age_s = Math.floor(Date.now() / 1000) - anchor;
    if (out.coverage_age_s >= COVERAGE_WINDOW_S) {
      out.stale = true;
      out.reasons.push(`checkpoint coverage ${Math.round(out.coverage_age_s / 3600)}h old (deadline ${out.coverage_deadline})`);
    } else if (out.coverage_age_s >= COVERAGE_OVERDUE_S) {
      out.refresh_overdue = true;
      const left = Math.round((out.coverage_deadline - Math.floor(Date.now() / 1000)) / 3600);
      out.reasons.push(`refresh overdue: checkpoint coverage ${Math.round(out.coverage_age_s / 3600)}h old, stale in ${left}h`);
    }
  }

  // End-to-end probe: exercise the real serving path — replica, snapshot,
  // overlay and ranking — rather than trusting a component's self-report.
  //
  // The handle must be one we are certain is in the corpus, or the canary cries
  // wolf. The first version probed the service's own account and reported a
  // failure on its first run, because that account has never been indexed. This
  // one is the same handle smoke.py asserts must rank first for its own query,
  // so if it ever stops being present, that is a real finding either way.
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

  let previous: FreshnessLevel | null = null;
  try {
    const prev = await env.KV.get<{ stale?: boolean; refresh_overdue?: boolean }>("freshness", "json");
    if (prev?.stale === true || prev?.stale === false) {
      previous = freshnessLevel({ stale: prev.stale, refresh_overdue: prev.refresh_overdue === true });
    }
  } catch { /* no prior state — treat as a change only if not fresh */ }

  const level = freshnessLevel(f);
  if (previous === level) return;                         // no change
  if (previous === null && level === "fresh") return;     // first run and healthy

  const lag = f.replica_lag_s === null ? "?" : `${Math.round(f.replica_lag_s / 60)}m`;
  const snap = f.snapshot_age_s === null ? "?" : `${Math.round(f.snapshot_age_s / 3600)}h`;
  const reasons = ["```", f.reasons.join("\n") || "unknown", "```"];
  const content = level === "stale"
    ? [
        "**typeahead: serving is stale**",
        ...reasons,
        `replica lag ${lag} · snapshot ${snap} · probe ${f.probe_ok ? "ok" : "FAILING"}`,
        "https://typeahead.waow.tech/health/freshness",
      ].join("\n")
    : level === "overdue"
      ? [
          "**typeahead: refresh missed — act before the slot goes stale**",
          ...reasons,
          "Search is still serving. Check the latest typeahead-index run: https://prefect-server.waow.tech/deployments",
          "https://typeahead.waow.tech/health/freshness",
        ].join("\n")
      : `**typeahead: serving is fresh again** — replica lag ${lag}, snapshot ${snap}`;

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

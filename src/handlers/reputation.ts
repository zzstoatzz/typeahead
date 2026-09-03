import type { TursoDB } from "../db";
import { percentileFromQuantiles, queryLabelers } from "../labelers";
import { json, computeAuthorityScoreBreakdown, avatarUrl, avatarFallbackUrl } from "../utils";
import { BSKY_MOD_DID, MOD_HIDE_VALS, resolveHidden } from "../moderation";
import { enrichProfileDids } from "./admin";
import type { Env } from "../types";

interface ReputationActorRow {
  did: string;
  handle: string;
  display_name: string;
  avatar_url: string;
  hidden: number;
  labels: string;
  created_at: string;
  associated: string;
  pds: string;
  followers_count: number;
  follows_count: number;
  posts_count: number;
  quality_score: number;
  updated_at: number;
  profile_checked_at: number;
  profile_source: string;
}

interface ScorePercentile {
  cohort: "snapshot_actors";
  percentile: number | null;
  topPercent: number | null;
  rank: number | null;
  total: number;
  above: number;
  belowOrEqual: number;
  note: string;
}

function parseLabels(raw: string): any[] {
  try {
    const labels = JSON.parse(raw || "[]");
    return Array.isArray(labels) ? labels : [];
  } catch {
    return [];
  }
}

function toIsoSeconds(sec: number | null | undefined): string | null {
  if (!sec) return null;
  return new Date(sec * 1000).toISOString();
}

function moderationLabels(labels: any[]) {
  return labels.map((l) => ({
    src: l?.src ?? "",
    val: l?.val ?? "",
    neg: Boolean(l?.neg),
    exp: l?.exp ?? null,
    activeSearchHide: !l?.neg && l?.src === BSKY_MOD_DID && MOD_HIDE_VALS.has(l?.val),
  }));
}

/** Percentile from the attached snapshot's p0..p100 quantiles (`/index/status`).
 *  The previous version ran two COUNT(*) queries over the visible corpus per
 *  report; the quantiles are computed once per build, off the request path. */
async function scorePercentile(searchBase: string | undefined, score: number): Promise<ScorePercentile> {
  const none = (note: string): ScorePercentile => ({
    cohort: "snapshot_actors", percentile: null, topPercent: null, rank: null, total: 0, above: 0, belowOrEqual: 0, note,
  });
  if (score <= 0) return none("Percentile is only computed for actors with a positive stored quality_score.");
  if (!searchBase) return none("Percentile unavailable: no search backend configured.");

  let status: { score_quantiles?: number[] | null; actor_count?: number } | null = null;
  try {
    const r = await fetch(`${searchBase}/index/status`, { signal: AbortSignal.timeout(2000) });
    if (r.ok) status = await r.json();
  } catch { /* the note below says so */ }
  const quantiles = status?.score_quantiles ?? null;
  const total = Number(status?.actor_count) || 0;
  const p = quantiles ? percentileFromQuantiles(score, quantiles) : null;
  if (p === null || total === 0) {
    return none("Percentile unavailable: the attached snapshot predates score quantiles (they arrive with the next index build).");
  }
  const above = Math.round(total * (100 - p) / 100);
  return {
    cohort: "snapshot_actors",
    percentile: p,
    topPercent: Math.max(0.01, Math.round((100 - p) * 100) / 100),
    rank: above + 1,
    total,
    above,
    belowOrEqual: total - above,
    note: "Percentile band among the actors in the attached index snapshot (visible at build time), to the nearest whole percent.",
  };
}

async function liveSearchPosition(
  searchBase: string | undefined,
  termRaw: string,
  did: string,
  limit = 50,
): Promise<{ query: string; found: boolean; position: number | null; matchedBy: string | null }> {
  const term = String(termRaw || "").trim();
  const miss = { query: term, found: false, position: null, matchedBy: null };
  if (!term || !searchBase) return miss;
  try {
    const res = await fetch(
      searchBase.replace(/\/$/, "") + "/search?q=" + encodeURIComponent(term) + "&limit=" + limit,
      { signal: AbortSignal.timeout(8000), headers: { "X-Client": "typeahead/reputation" } },
    );
    if (!res.ok) return miss;
    const data = await res.json<{ actors?: { did: string }[] }>();
    const idx = (data.actors || []).findIndex((a) => a.did === did);
    if (idx < 0) return miss;
    return { query: term, found: true, position: idx + 1, matchedBy: "live search" };
  } catch {
    return { query: term, found: false, position: null, matchedBy: "timed out" };
  }
}

export async function handleReputation(request: Request, db: TursoDB, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const actor = (url.searchParams.get("actor") || url.searchParams.get("handle") || url.searchParams.get("did") || "").trim();
  if (!actor) return json({ error: "actor query param required" }, 400);

  const row = await db.prepare(
    `SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, pds,
            followers_count, follows_count, posts_count, quality_score, updated_at, profile_checked_at, profile_source, profile_source
     FROM actors
     WHERE did = ?1 OR handle = ?1 COLLATE NOCASE
     LIMIT 1`
  ).bind(actor).first<ReputationActorRow>();

  if (!row) return json({ error: "actor not found" }, 404);

  // On-view enrichment: an actor someone is literally looking at is the best
  // possible enrichment candidate (same demand-driven principle as 59f6e39's
  // immediate request-indexing). Best-effort: failures fall back to the
  // stale row, with a labeled log line (never a silent collapsing catch).
  //
  // Concurrency: the conditional stamp below is an optimistic claim — turso's
  // single writer guarantees exactly one concurrent viewer gets changes=1 and
  // does the fetch; the rest proceed with the stale row and see fresh data on
  // their next view. The claim also stamps dead DIDs (bsky_404) out of the
  // queue up front, so an unresolvable actor costs one fetch ever instead of
  // one per view. Named tradeoff: a winner that dies mid-enrich leaves the
  // actor stamped-but-empty — the same exposure the cron's stamp-first
  // identity path accepts, for the same queue-pinning reason.
  let enrichedOnView = false;
  if (!row.profile_checked_at) {
    try {
      const claim = await db.prepare(
        "UPDATE actors SET profile_checked_at = unixepoch() WHERE did = ?1 AND profile_checked_at = 0"
      ).bind(row.did).run();
      if ((claim.meta?.changes ?? 0) === 1) {
        const r = await enrichProfileDids(db, env, [row.did]);
        if (r.written > 0) {
          const fresh = await db.prepare(
            `SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, pds,
                    followers_count, follows_count, posts_count, quality_score, updated_at, profile_checked_at
             FROM actors WHERE did = ?1 LIMIT 1`
          ).bind(row.did).first<ReputationActorRow>();
          if (fresh) {
            Object.assign(row, fresh);
            enrichedOnView = true;
          }
        } else if (r.errors.length > 0) {
          console.log(JSON.stringify({ event: "enrich_on_view_miss", did: row.did, reason: r.errors[0]?.reason }));
        }
      }
    } catch (e) {
      console.log(JSON.stringify({ event: "enrich_on_view_failed", did: row.did, error: String(e).slice(0, 120) }));
    }
  }

  const labels = parseLabels(row.labels);
  const override = await db.prepare(
    "SELECT action, reason, created_at FROM mod_overrides WHERE did = ?1"
  ).bind(row.did).first<{ action: "show" | "hide"; reason: string; created_at: number }>();
  const blockedDomain = await db.prepare(
    `SELECT domain, reason, created_at FROM blocked_domains
     WHERE lower(?1) = domain OR lower(?1) LIKE '%.' || domain
     ORDER BY length(domain) DESC LIMIT 1`
  ).bind(row.handle || "").first<{ domain: string; reason: string; created_at: number }>();
  const effectiveHidden = resolveHidden({
    labels,
    handle: row.handle,
    override: override?.action,
    domainBlocked: Boolean(blockedDomain),
  });

  const computed = computeAuthorityScoreBreakdown({
    followersCount: row.followers_count,
    followsCount: row.follows_count,
    postsCount: row.posts_count,
    createdAt: row.created_at,
  });

  const storedScore = Number(row.quality_score) || 0;
  const handlePrefix = row.handle.split(".")[0] || row.handle.slice(0, 3);
  const searchBase = env.SEARCH_BACKEND_URL;
  const [exactHandle, displayName, handlePrefixPosition, exactHandleExists, percentile, labelers] = await Promise.all([
    liveSearchPosition(searchBase, row.handle, row.did),
    row.display_name ? liveSearchPosition(searchBase, row.display_name, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    handlePrefix ? liveSearchPosition(searchBase, handlePrefix, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    db.prepare(
      "SELECT did FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1"
    ).bind(row.handle).first<{ did: string }>(),
    scorePercentile(searchBase, storedScore),
    queryLabelers(row.did),
  ]);

  return json({
    actor: {
      did: row.did,
      handle: row.handle,
      displayName: row.display_name,
      avatar: row.avatar_url ? avatarUrl(row.did, row.avatar_url, row.pds) : null,
      avatarFallback: row.avatar_url ? avatarFallbackUrl(row.did, row.avatar_url, row.pds) : null,
      createdAt: row.created_at || null,
      pds: row.pds || null,
    },
    score: {
      stored: storedScore,
      computedNow: computed.score,
      staleDelta: Math.round((computed.score - storedScore) * 100) / 100,
      percentile,
      breakdown: computed,
    },
    moderation: {
      // what THIS service decided and why, then what each moderation labeler
      // says right now. The stored labels are only what the unauthenticated
      // appview reports: self-labels plus Bluesky's own moderation service.
      storedLabelsSource: "self-labels + Bluesky Moderation Service, as the unauthenticated appview reports them",
      labelers,
      hiddenStored: Boolean(row.hidden),
      hiddenComputed: Boolean(effectiveHidden),
      labels: moderationLabels(labels),
      domainBlocked: blockedDomain ? {
        domain: blockedDomain.domain,
        reason: blockedDomain.reason || "",
        createdAt: toIsoSeconds(blockedDomain.created_at),
      } : null,
      override: override ? {
        action: override.action,
        reason: override.reason || "",
        createdAt: toIsoSeconds(override.created_at),
      } : null,
    },
    indexing: {
      enrichedOnView,
      updatedAt: toIsoSeconds(row.updated_at),
      profileCheckedAt: toIsoSeconds(row.profile_checked_at),
      // 'bsky' | 'blacksky' | 'pds' | '' (written before the column existed)
      profileSource: row.profile_source || "",
      shouldBeInPrefixIndex: Boolean(row.handle && !effectiveHidden),
      exactHandleLookupFindsIt: exactHandleExists?.did === row.did,
      exactHandleLookupDid: exactHandleExists?.did ?? null,
    },
    sampleQueryPositions: {
      exactHandle,
      displayName,
      handlePrefix: handlePrefixPosition,
    },
  });
}

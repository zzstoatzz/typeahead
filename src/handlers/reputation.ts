import type { TursoDB } from "../db";
import { json, sanitize, computeAuthorityScoreBreakdown, avatarUrl } from "../utils";
import { BSKY_MOD_DID, MOD_HIDE_VALS, resolveHidden } from "../moderation";

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
}

interface SearchCandidate {
  did: string;
  handle: string;
  display_name: string;
}

interface ScorePercentile {
  cohort: "visible_scored";
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

async function scorePercentile(db: TursoDB, score: number): Promise<ScorePercentile> {
  if (score <= 0) {
    return {
      cohort: "visible_scored",
      percentile: null,
      topPercent: null,
      rank: null,
      total: 0,
      above: 0,
      belowOrEqual: 0,
      note: "Percentile is only computed for actors with a positive stored quality_score.",
    };
  }

  let row: { total: number } | null = null;
  let aboveRow: { above: number } | null = null;
  try {
    row = await db.prepare(
      "SELECT COUNT(*) AS total FROM actors INDEXED BY idx_actors_visible_quality_score WHERE handle != '' AND hidden = 0 AND quality_score > 0"
    ).first<{ total: number }>();
    aboveRow = await db.prepare(
      "SELECT COUNT(*) AS above FROM actors INDEXED BY idx_actors_visible_quality_score WHERE handle != '' AND hidden = 0 AND quality_score > 0 AND quality_score > ?1"
    ).bind(score).first<{ above: number }>();
  } catch {
    return {
      cohort: "visible_scored",
      percentile: null,
      topPercent: null,
      rank: null,
      total: 0,
      above: 0,
      belowOrEqual: 0,
      note: "Percentile unavailable: the visible quality_score index is not present yet.",
    };
  }

  const total = Number(row?.total) || 0;
  const above = Number(aboveRow?.above) || 0;
  const belowOrEqual = Math.max(0, total - above);
  const percentile = total > 0 ? Math.round((belowOrEqual / total) * 10000) / 100 : null;
  const topPercent = total > 0 ? Math.round(((above + 1) / total) * 10000) / 100 : null;
  return {
    cohort: "visible_scored",
    percentile,
    topPercent,
    rank: total > 0 ? above + 1 : null,
    total,
    above,
    belowOrEqual,
    note: "Percentile is among visible actors with a positive stored quality_score, not every actor in the corpus.",
  };
}



/** Sample position via the real search engine (bounded prefix-index lookups,
 *  ms-fast) instead of recreating search as raw SQL — the FTS recreation
 *  ranked every match before LIMIT and took 55-85s for short/common tokens,
 *  blowing past the edge request limit entirely (2026-08-04). An 8s timebox
 *  keeps a slow engine from wedging the report; timeouts read as unranked. */
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
    const data = (await res.json()) as { actors?: { did: string }[] };
    const idx = (data.actors || []).findIndex((a) => a.did === did);
    if (idx < 0) return miss;
    return { query: term, found: true, position: idx + 1, matchedBy: "live search" };
  } catch {
    return { query: term, found: false, position: null, matchedBy: "timed out" };
  }
}

export async function handleReputation(request: Request, db: TursoDB, env: { SEARCH_BACKEND_URL?: string }): Promise<Response> {
  const url = new URL(request.url);
  const actor = (url.searchParams.get("actor") || url.searchParams.get("handle") || url.searchParams.get("did") || "").trim();
  if (!actor) return json({ error: "actor query param required" }, 400);

  const row = await db.prepare(
    `SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, pds,
            followers_count, follows_count, posts_count, quality_score, updated_at, profile_checked_at
     FROM actors
     WHERE did = ?1 OR handle = ?1 COLLATE NOCASE
     LIMIT 1`
  ).bind(actor).first<ReputationActorRow>();

  if (!row) return json({ error: "actor not found" }, 404);

  const labels = parseLabels(row.labels);
  const override = await db.prepare(
    "SELECT action, reason, created_at FROM mod_overrides WHERE did = ?1"
  ).bind(row.did).first<{ action: string; reason: string; created_at: number }>();
  const blockedDomain = await db.prepare(
    `SELECT domain, reason, created_at FROM blocked_domains
     WHERE lower(?1) = domain OR lower(?1) LIKE '%.' || domain
     ORDER BY length(domain) DESC LIMIT 1`
  ).bind(row.handle || "").first<{ domain: string; reason: string; created_at: number }>();
  const effectiveHidden = resolveHidden({
    labels,
    handle: row.handle,
    override: override?.action as "show" | "hide" | null | undefined,
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
  const [exactHandle, displayName, handlePrefixPosition, exactHandleExists, percentile] = await Promise.all([
    liveSearchPosition(searchBase, row.handle, row.did),
    row.display_name ? liveSearchPosition(searchBase, row.display_name, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    handlePrefix ? liveSearchPosition(searchBase, handlePrefix, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    db.prepare(
      "SELECT did FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1"
    ).bind(row.handle).first<{ did: string }>(),
    scorePercentile(db, storedScore),
  ]);

  return json({
    actor: {
      did: row.did,
      handle: row.handle,
      displayName: row.display_name,
      avatar: row.avatar_url ? avatarUrl(row.did, row.avatar_url, row.pds) : null,
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
      updatedAt: toIsoSeconds(row.updated_at),
      profileCheckedAt: toIsoSeconds(row.profile_checked_at),
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

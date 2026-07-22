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

async function mergedFallbackPosition(
  db: TursoDB,
  termRaw: string,
  did: string,
  limit = 50,
): Promise<{ query: string; found: boolean; position: number | null; matchedBy: string | null }> {
  const term = sanitize(termRaw);
  if (!term) return { query: termRaw, found: false, position: null, matchedBy: null };

  const ftsQuery = `"${term}"*`;
  const overfetch = Math.min(limit * 5, 250);
  const [exactRes, prefixRes, ftsRes] = await db.batch([
    db.prepare(
      `SELECT did, handle, display_name
       FROM actors WHERE handle = ?1 COLLATE NOCASE AND hidden = 0 LIMIT 1`
    ).bind(term),
    db.prepare(
      `SELECT did, handle, display_name
       FROM actors WHERE handle LIKE ?1 COLLATE NOCASE AND handle != ?2 COLLATE NOCASE AND hidden = 0
       LIMIT ?3`
    ).bind(term + "%", term, overfetch),
    db.prepare(
      `SELECT a.did, a.handle, a.display_name
       FROM actors_fts
       JOIN actors a ON a.rowid = actors_fts.rowid
       WHERE actors_fts MATCH ?1 AND a.handle != '' AND a.hidden = 0
       ORDER BY rank
       LIMIT ?2`
    ).bind(ftsQuery, overfetch),
  ], "read");

  const exactRows = (exactRes.results as SearchCandidate[]) || [];
  const prefixRows = ((prefixRes.results as SearchCandidate[]) || [])
    .slice()
    .sort((a, b) => a.handle.length - b.handle.length);
  const prefixDids = new Set(prefixRows.map((r) => r.did));
  const ftsRows = ((ftsRes.results as SearchCandidate[]) || [])
    .filter((r) => !prefixDids.has(r.did));

  const seen = new Set<string>();
  const merged: { row: SearchCandidate; matchedBy: string }[] = [];
  for (const row of exactRows) {
    if (seen.has(row.did)) continue;
    seen.add(row.did);
    merged.push({ row, matchedBy: "exact_handle" });
    if (merged.length >= limit) break;
  }

  let pi = 0, fi = 0;
  let takePrefix = true;
  while (merged.length < limit && (pi < prefixRows.length || fi < ftsRows.length)) {
    let row: SearchCandidate | null = null;
    let matchedBy = "prefix";
    if (takePrefix) {
      while (pi < prefixRows.length && seen.has(prefixRows[pi].did)) pi++;
      if (pi < prefixRows.length) {
        row = prefixRows[pi++];
        matchedBy = "handle_prefix";
      } else {
        while (fi < ftsRows.length && seen.has(ftsRows[fi].did)) fi++;
        if (fi < ftsRows.length) {
          row = ftsRows[fi++];
          matchedBy = "fts";
        }
      }
    } else {
      while (fi < ftsRows.length && seen.has(ftsRows[fi].did)) fi++;
      if (fi < ftsRows.length) {
        row = ftsRows[fi++];
        matchedBy = "fts";
      } else {
        while (pi < prefixRows.length && seen.has(prefixRows[pi].did)) pi++;
        if (pi < prefixRows.length) {
          row = prefixRows[pi++];
          matchedBy = "handle_prefix";
        }
      }
    }
    if (!row) break;
    seen.add(row.did);
    merged.push({ row, matchedBy });
    takePrefix = !takePrefix;
  }

  const idx = merged.findIndex((r) => r.row.did === did);
  return {
    query: term,
    found: idx >= 0,
    position: idx >= 0 ? idx + 1 : null,
    matchedBy: idx >= 0 ? merged[idx].matchedBy : null,
  };
}

export async function handleReputation(request: Request, db: TursoDB): Promise<Response> {
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
  const [exactHandle, displayName, handlePrefixPosition, exactHandleExists, percentile] = await Promise.all([
    mergedFallbackPosition(db, row.handle, row.did),
    row.display_name ? mergedFallbackPosition(db, row.display_name, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    handlePrefix ? mergedFallbackPosition(db, handlePrefix, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
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

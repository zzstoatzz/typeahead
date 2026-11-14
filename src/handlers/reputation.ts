import { percentileFromQuantiles, queryLabelers } from "../labelers";
import { json, computeAuthorityScoreBreakdown, avatarUrl, avatarFallbackUrl } from "../utils";
import { BSKY_MOD_DID, MOD_HIDE_VALS, resolveHidden } from "../moderation";
import type { Env } from "../types";
import { controlDb } from "../control-store";
import { slotActors, SlotReadError, type SlotActor } from "../slot-read";

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

type OverrideNote = { action: "show" | "hide"; reason: string; created_at: number };
type DomainNote = { domain: string; reason: string; created_at: number };

/** Everything the report says about one actor that comes from a store. */
interface StoredReputation {
  actor: SlotActor;
  override: OverrideNote | null;
  blockedDomain: DomainNote | null;
  /** the visible actor an exact lookup of the handle resolves to */
  exactHandleDid: string | null;
}

const OVERRIDE_SQL = "SELECT action, reason, updated_at AS created_at FROM mod_overrides WHERE did = ?1";
const BLOCKED_DOMAIN_SQL = `SELECT domain, reason, updated_at AS created_at FROM blocked_domains
     WHERE lower(?1) = domain OR lower(?1) LIKE '%.' || domain
     ORDER BY length(domain) DESC LIMIT 1`;

/** The actor from the routed store slot, with the override and the blocked
 *  domain that apply to it from the control database. */
async function slotReputation(env: Env, actor: string): Promise<StoredReputation | null> {
  const [found] = await slotActors(env, [actor]);
  if (!found) return null;
  const sameHandle = await slotActors(env, [found.handle]);
  const control = controlDb(env);
  const override = await control.prepare(OVERRIDE_SQL).bind(found.did).first<OverrideNote>();
  const blockedDomain = await control.prepare(BLOCKED_DOMAIN_SQL).bind(found.handle).first<DomainNote>();
  return {
    actor: found,
    override,
    blockedDomain,
    exactHandleDid: sameHandle.find((candidate) => !candidate.hidden)?.did ?? null,
  };
}

export async function handleReputation(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const actor = (url.searchParams.get("actor") || url.searchParams.get("handle") || url.searchParams.get("did") || "").trim();
  if (!actor) return json({ error: "actor query param required" }, 400);

  let stored: StoredReputation | null;
  try {
    stored = await slotReputation(env, actor);
  } catch (error) {
    if (!(error instanceof SlotReadError)) throw error;
    console.log(JSON.stringify({ event: "reputation_slot_failed", status: error.status, error: error.message }));
    return json({ error: "actor lookup is temporarily unavailable" }, 503);
  }
  if (!stored) return json({ error: "actor not found" }, 404);
  const { actor: row, override, blockedDomain } = stored;

  const labels = row.labels;
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
  const [exactHandle, displayName, handlePrefixPosition, percentile, labelers] = await Promise.all([
    liveSearchPosition(searchBase, row.handle, row.did),
    row.display_name ? liveSearchPosition(searchBase, row.display_name, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
    handlePrefix ? liveSearchPosition(searchBase, handlePrefix, row.did) : Promise.resolve({ query: "", found: false, position: null, matchedBy: null }),
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
      hiddenStored: row.hidden_stored,
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
      // a slot keeps whether a profile was read, not when or from where, and
      // fetches nothing on view: enrichedOnView, profileCheckedAt and
      // profileSource are constants the report page still reads
      enrichedOnView: false,
      updatedAt: toIsoSeconds(row.updated_at),
      profileCheckedAt: null,
      profileChecked: row.profile_checked,
      profileSource: "",
      shouldBeInPrefixIndex: Boolean(row.handle && !effectiveHidden),
      exactHandleLookupFindsIt: stored.exactHandleDid === row.did,
      exactHandleLookupDid: stored.exactHandleDid,
    },
    sampleQueryPositions: {
      exactHandle,
      displayName,
      handlePrefix: handlePrefixPosition,
    },
  });
}

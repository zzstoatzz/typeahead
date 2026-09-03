import { CORS_HEADERS } from "./types";
import { resolveHidden } from "./moderation";

export function clientIP(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function json<T extends object>(data: T, status = 200, extra?: Record<string, string>): Response {
  return Response.json(data, { status, headers: { ...CORS_HEADERS, ...extra } });
}

/** Primary avatar URL: bsky's CDN for everyone. It serves any actor the
 *  appview has ingested (the overwhelming majority, self-hosted PDSes
 *  included), stays up, and matches what bsky's own typeahead returns —
 *  we are a drop-in for that endpoint. The old behavior pointed non-bsky
 *  actors at their own PDS's getBlob, which (a) broke whenever a hobbyist
 *  box was down (evil.brookie.blog via pegasus.juli.ee, 2026-08-04) and
 *  (b) leaked every viewer's IP to a stranger's server just for rendering
 *  a dropdown. For actors the appview has never seen (tangled-only etc.)
 *  the CDN 404s — that's what avatarFallbackUrl is for. */
export function avatarUrl(did: string, cidOrUrl: string, _pds?: string): string {
  // rows store either a bare CID or a full URL (older PDS-fallback enrichment
  // stored complete getBlob URLs) — extract the CID from any shape and rebuild
  // the CDN form, so historic pegasus-style rows get the CDN too.
  const cid = extractAvatarCid(cidOrUrl);
  if (cid) return `https://cdn.bsky.app/img/avatar/plain/${did}/${cid}`;
  return cidOrUrl;
}

export function bannerUrl(did: string, cidOrUrl: string): string {
  const cid = extractAvatarCid(cidOrUrl);
  if (cid) return `https://cdn.bsky.app/img/banner/plain/${did}/${cid}`;
  return cidOrUrl;
}

/** PDS-direct blob URL — the only avatar source for actors outside the bsky
 *  appview. Served as an extra `avatarFallback` field; our own pages swap to
 *  it on img error. Third-party lexicon consumers ignore unknown fields. */
export function avatarFallbackUrl(did: string, cidOrUrl: string, pds?: string): string | null {
  // a stored non-CDN https URL IS a pds-direct source — keep it verbatim
  if (cidOrUrl.startsWith("https://")) {
    return cidOrUrl.includes("cdn.bsky.app") ? null : cidOrUrl;
  }
  if (!pds || pds.includes(".bsky.network")) return null;
  const origin = pdsOrigin(pds);
  if (!origin) return null;
  const cid = extractAvatarCid(cidOrUrl);
  if (!cid) return null;
  return `${origin}/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(did)}&cid=${encodeURIComponent(cid)}`;
}

/** Normalize an actor-controlled PDS value to a plain https origin, or null.
 *  The pds column comes from the actor's own identity doc (PLC/did:web), so
 *  anything built from it — fetch targets in enrichment, blob URLs handed to
 *  clients — must not smuggle credentials, paths, or non-https schemes, and
 *  single-label hosts (localhost, internal names) are rejected outright. */
export function pdsOrigin(pds: string): string | null {
  let u: URL;
  try {
    u = new URL(pds);
  } catch {
    return null;
  }
  if (u.protocol !== "https:") return null;
  if (u.username || u.password) return null;
  if ((u.pathname !== "/" && u.pathname !== "") || u.search || u.hash) return null;
  if (!u.hostname.includes(".")) return null;
  return u.origin;
}

export function extractAvatarCid(urlOrCid: string): string {
  if (!urlOrCid) return '';
  // bare CID (from PDS fallback) — no slashes
  if (!urlOrCid.includes('/')) return urlOrCid;
  // PDS getBlob URL — extract cid param
  const cidParam = urlOrCid.match(/[?&]cid=([^&]+)/);
  if (cidParam) return decodeURIComponent(cidParam[1]);
  // bsky CDN URL — extract last path segment
  const match = urlOrCid.match(/\/([^/]+?)(?:@[a-z]+)?$/);
  return match?.[1] ?? '';
}

/** Blacksky's appview (did:web:api.blacksky.community). It indexes the whole
 *  network, answers getProfile without auth, and keeps serving accounts
 *  Bluesky's appview refuses with AccountTakedown — so it is the only place
 *  a Bluesky-banned, Blacksky-hosted account's follower and post counts can
 *  be read from (Łink, 2026-09-03: 25.8k followers there, 0 here). Its
 *  follower counts run lower than Bluesky's for the same account (it indexes
 *  a narrower follow graph), which is why rows carry `profile_source`. */
export const BLACKSKY_APPVIEW = 'https://api.blacksky.community';

/** which appview or record a row's profile fields were last written from */
export type ProfileSource = 'bsky' | 'blacksky' | 'pds';

/** the Blacksky appview's profileViewDetailed for `did`, or null if it has none */
export async function fetchProfileFromBlacksky(did: string): Promise<any | null> {
  try {
    const res = await fetch(
      `${BLACKSKY_APPVIEW}/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(did)}`,
      { signal: AbortSignal.timeout(5000) },
    );
    if (!res.ok) return null;
    const profile: any = await res.json();
    return profile?.did === did ? profile : null;
  } catch {
    return null;
  }
}

/** fetch profile directly from an actor's PDS — tries bsky profile first, then tangled.
 *  returns bare CID for avatar (not full URL) — caller reconstructs at query time via pds column. */
export async function fetchProfileFromPds(did: string, pds: string): Promise<any | null> {
  const origin = pdsOrigin(pds);
  if (!origin) return null;
  const collections = ['app.bsky.actor.profile', 'sh.tangled.actor.profile'];
  for (const collection of collections) {
    try {
      const res = await fetch(
        `${origin}/xrpc/com.atproto.repo.getRecord?repo=${encodeURIComponent(did)}&collection=${collection}&rkey=self`
      );
      if (!res.ok) continue;
      const data: any = await res.json();
      const val = data?.value;
      if (!val) continue;

      return {
        did,
        handle: '',
        displayName: val.displayName || '',
        avatar: val.avatar?.ref?.$link || '',
        labels: [],
        createdAt: val.createdAt || '',
        associated: {},
      };
    } catch {
      continue;
    }
  }
  return null;
}

/** `app.bsky.actor.defs#profileAssociated` as bsky sends it (all fields optional). */
export interface ProfileAssociated {
  lists?: number | null;
  feedgens?: number | null;
  starterPacks?: number | null;
  labeler?: boolean | null;
  chat?: { allowIncoming: string } | null;
  activitySubscription?: { allowSubscriptions: string } | null;
}

/** strip zero/false fields from associated object to match bsky's typeahead shape */
export function cleanAssociated(assoc: ProfileAssociated | null | undefined): ProfileAssociated {
  const clean: ProfileAssociated = {};
  if (!assoc) return clean;
  if (assoc.lists) clean.lists = assoc.lists;
  if (assoc.feedgens) clean.feedgens = assoc.feedgens;
  if (assoc.starterPacks) clean.starterPacks = assoc.starterPacks;
  if (assoc.labeler) clean.labeler = assoc.labeler;
  if (assoc.chat) clean.chat = assoc.chat;
  if (assoc.activitySubscription) clean.activitySubscription = assoc.activitySubscription;
  return clean;
}

/** extract the fields we store from a bsky profile response (getProfiles/getProfile/typeahead).
 *  Count fields come from profileViewDetailed (getProfile/getProfiles); they default to 0 when
 *  absent so PDS-fallback profiles (no counts) don't introduce undefineds. Writers use a
 *  COALESCE(NULLIF(?, 0), existing) pattern so a 0-bind from PDS preserves the previous value. */
export interface ProfileFields {
  handle: string;
  displayName: string;
  avatarCid: string;
  description: string;
  bannerCid: string;
  labels: string;
  hidden: number;
  createdAt: string;
  associated: string;
  followersCount: number;
  followsCount: number;
  postsCount: number;
  qualityScore: number;
}

export interface AuthorityScoreBreakdown {
  score: number;
  followersInfluence: number;
  postsInfluence: number;
  followerFollowRatioBonus: number;
  matureAccountBonus: number;
  freshness: {
    ageDays: number | null;
    bucket: "unknown" | "<24h" | "<72h" | "mature";
    penalty: number;
    note: string;
  };
  inputs: {
    followersCount: number;
    followsCount: number;
    postsCount: number;
    createdAt?: string;
  };
}

/**
 * Authority/quality score — our own ranking signal computed from the data we have.
 * Not bsky's pagerank, but a defensible proxy that promotes real accounts over
 * spam without requiring graph-derived rank.
 *
 *   log10(followers+1) * 100   base influence (saturating)
 *   log10(posts+1) * 20        productivity (saturating)
 *   +50 / +100                 follower:follow ratio (real accounts follow fewer than follow them)
 *   +30 / +60                  age bonuses for 1y+ / 3y+ accounts
 *
 * Typical output range: spam ~80, normal ~200-400, popular ~600-900, top ~1000+.
 * Search uses this as the dominant non-lexical signal so popular accounts win
 * lexical ties without unrelated accounts surfacing.
 */
export function computeAuthorityScoreBreakdown(p: {
  followersCount: number;
  followsCount: number;
  postsCount: number;
  createdAt?: string;
}, nowMs = Date.now()): AuthorityScoreBreakdown {
  const f = Math.max(0, Number(p.followersCount) || 0);
  const fw = Math.max(0, Number(p.followsCount) || 0);
  const pc = Math.max(0, Number(p.postsCount) || 0);

  const followersInfluence = Math.log10(f + 1) * 100;
  const postsInfluence = Math.log10(pc + 1) * 20;
  let followerFollowRatioBonus = 0;
  let matureAccountBonus = 0;
  let freshnessPenalty = 0;
  let ageDays: number | null = null;
  let bucket: AuthorityScoreBreakdown["freshness"]["bucket"] = "unknown";

  if (f > 100 && fw > 0) {
    const ratio = f / fw;
    if (ratio > 10) followerFollowRatioBonus += 50;
    if (ratio > 100) followerFollowRatioBonus += 100;
  }

  if (p.createdAt) {
    const t = Date.parse(p.createdAt);
    if (!Number.isNaN(t)) {
      ageDays = (nowMs - t) / 86400000;
      if (ageDays < 1) {
        bucket = "<24h";
        freshnessPenalty = -100;
      } else if (ageDays < 3) {
        bucket = "<72h";
        freshnessPenalty = -50;
      } else {
        bucket = "mature";
      }
      if (ageDays > 365) matureAccountBonus += 30;
      if (ageDays > 365 * 3) matureAccountBonus += 30;
    }
  }

  const score = Math.max(0, followersInfluence + postsInfluence + followerFollowRatioBonus + matureAccountBonus + freshnessPenalty);
  const inputs: AuthorityScoreBreakdown["inputs"] = {
    followersCount: f,
    followsCount: fw,
    postsCount: pc,
  };
  if (p.createdAt) inputs.createdAt = p.createdAt;

  return {
    score: Math.round(score * 100) / 100,
    followersInfluence: Math.round(followersInfluence * 100) / 100,
    postsInfluence: Math.round(postsInfluence * 100) / 100,
    followerFollowRatioBonus,
    matureAccountBonus,
    freshness: {
      ageDays: ageDays === null ? null : Math.round(ageDays * 100) / 100,
      bucket,
      penalty: freshnessPenalty,
      note: "Fresh accounts are penalized: -100 for <24h old, -50 for <72h old. Scores are floored at 0.",
    },
    inputs,
  };
}

export function computeAuthorityScore(p: {
  followersCount: number;
  followsCount: number;
  postsCount: number;
  createdAt?: string;
}): number {
  return computeAuthorityScoreBreakdown(p).score;
}

export function extractProfileFields(
  profile: any,
  override?: 'show' | 'hide' | null,
  domainBlocked?: boolean,
): ProfileFields {
  const hidden = resolveHidden({
    labels: profile.labels,
    handle: profile.handle,
    override,
    domainBlocked,
  });
  const raw = profile.avatar || '';
  const avatarCid = extractAvatarCid(raw);
  const followersCount = Number(profile.followersCount) || 0;
  const followsCount = Number(profile.followsCount) || 0;
  const postsCount = Number(profile.postsCount) || 0;
  const createdAt = profile.createdAt || '';

  // Did this profile actually carry appview aggregates? A countless response
  // (transient appview gap, PDS-record fallback) omits these fields entirely.
  // We must distinguish that from a genuine zero-follower account (which sends
  // followersCount: 0). Scoring a countless profile yields only the age bonus
  // (30/60) — non-zero, so it would sail through every `NULLIF(?,0)` write
  // guard and CLOBBER a real account's score while the real follower count is
  // preserved. Emit 0 instead so the same guards keep the prior good score.
  // (This froze zzstoatzz.io at 30 and pfrazee.com at 60 despite 2k/308k
  // followers — the counts survived, the score didn't.)
  const hasAggregates =
    profile.followersCount != null ||
    profile.followsCount != null ||
    profile.postsCount != null;

  return {
    handle: profile.handle || '',
    displayName: profile.displayName || '',
    avatarCid,
    description: profile.description || '',
    bannerCid: extractAvatarCid(profile.banner || ''),
    labels: JSON.stringify(profile.labels || []),
    hidden,
    createdAt,
    associated: JSON.stringify(cleanAssociated(profile.associated)),
    followersCount,
    followsCount,
    postsCount,
    qualityScore: hasAggregates
      ? computeAuthorityScore({ followersCount, followsCount, postsCount, createdAt })
      : 0,
  };
}

/**
 * Strip only what would break our SQL: LIKE wildcards (% _), the LIKE escape
 * (\), the FTS5 phrase-quote char ("), and control characters. Everything
 * else passes through — parens, brackets, colons, commas, quotes, emoji,
 * unicode, etc. all appear in real display names (e.g. "fig (aka:[phil])")
 * and stripping them silently turns a meaningful query into a useless one.
 *
 * Parameter binding (?) handles SQL injection. The FTS5 tokenizer treats
 * non-alphanumeric chars as token delimiters inside phrase quotes, so the
 * preserved characters don't break the FTS MATCH syntax.
 */
export function sanitize(q: string): string {
  return q.replace(/[\\%_"\p{Cc}]/gu, "").trim();
}

export function html(body: string, extra?: Record<string, string>, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS, ...extra },
  });
}

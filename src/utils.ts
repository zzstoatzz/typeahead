import { CORS_HEADERS } from "./types";
import { resolveHidden } from "./moderation";

export function clientIP(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: CORS_HEADERS });
}

export function avatarUrl(did: string, cidOrUrl: string, pds?: string): string {
  if (cidOrUrl.startsWith("https://")) return cidOrUrl;
  if (pds && !pds.includes('.bsky.network')) {
    return `${pds}/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(did)}&cid=${encodeURIComponent(cidOrUrl)}`;
  }
  return `https://cdn.bsky.app/img/avatar/plain/${did}/${cidOrUrl}`;
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

/** fetch profile directly from an actor's PDS — tries bsky profile first, then tangled.
 *  returns bare CID for avatar (not full URL) — caller reconstructs at query time via pds column. */
export async function fetchProfileFromPds(did: string, pds: string): Promise<any | null> {
  const collections = ['app.bsky.actor.profile', 'sh.tangled.actor.profile'];
  for (const collection of collections) {
    try {
      const res = await fetch(
        `${pds}/xrpc/com.atproto.repo.getRecord?repo=${encodeURIComponent(did)}&collection=${collection}&rkey=self`
      );
      if (!res.ok) continue;
      const data: any = await res.json();
      const val = data?.value;
      if (!val) continue;

      return {
        did,
        handle: '',
        displayName: val.displayName || '',
        avatar: val.avatar?.ref?.$link || val.avatar?.ref?.['$link'] || '',
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

/** strip zero/false fields from associated object to match bsky's typeahead shape */
export function cleanAssociated(assoc: any): Record<string, unknown> {
  if (!assoc || typeof assoc !== 'object') return {};
  const clean: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(assoc)) {
    if (v === 0 || v === false || v === null || v === undefined) continue;
    clean[k] = v;
  }
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
  labels: string;
  hidden: number;
  createdAt: string;
  associated: string;
  followersCount: number;
  followsCount: number;
  postsCount: number;
  qualityScore: number;
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
export function computeAuthorityScore(p: {
  followersCount: number;
  followsCount: number;
  postsCount: number;
  createdAt?: string;
}): number {
  const f = Math.max(0, Number(p.followersCount) || 0);
  const fw = Math.max(0, Number(p.followsCount) || 0);
  const pc = Math.max(0, Number(p.postsCount) || 0);

  let score = Math.log10(f + 1) * 100 + Math.log10(pc + 1) * 20;

  if (f > 100 && fw > 0) {
    const ratio = f / fw;
    if (ratio > 10) score += 50;
    if (ratio > 100) score += 100;
  }

  if (p.createdAt) {
    const t = Date.parse(p.createdAt);
    if (!Number.isNaN(t)) {
      const ageDays = (Date.now() - t) / 86400000;
      if (ageDays > 365) score += 30;
      if (ageDays > 365 * 3) score += 30;
    }
  }

  return Math.round(score * 100) / 100;
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
  return q.replace(/[\\%_"\x00-\x1f\x7f]/g, "").trim();
}

export function html(body: string, extra?: Record<string, string> | number, status = 200): Response {
  if (typeof extra === "number") { status = extra; extra = undefined; }
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS, ...extra },
  });
}

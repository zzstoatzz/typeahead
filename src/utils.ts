import { CORS_HEADERS } from "./types";
import { shouldHide, hasExplicitSlur } from "./moderation";

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
}

export function extractProfileFields(profile: any, override?: 'show' | 'hide' | null): ProfileFields {
  let hidden = shouldHide(profile.labels) ? 1 : 0;
  if (hasExplicitSlur(profile.handle || '')) hidden = 1;
  if (override === 'show') hidden = 0;
  if (override === 'hide') hidden = 1;
  const raw = profile.avatar || '';
  const avatarCid = extractAvatarCid(raw);
  return {
    handle: profile.handle || '',
    displayName: profile.displayName || '',
    avatarCid,
    labels: JSON.stringify(profile.labels || []),
    hidden,
    createdAt: profile.createdAt || '',
    associated: JSON.stringify(cleanAssociated(profile.associated)),
    followersCount: Number(profile.followersCount) || 0,
    followsCount: Number(profile.followsCount) || 0,
    postsCount: Number(profile.postsCount) || 0,
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

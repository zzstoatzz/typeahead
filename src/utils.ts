import { CORS_HEADERS } from "./types";
import { shouldHide } from "./moderation";

export function clientIP(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: CORS_HEADERS });
}

export function avatarUrl(did: string, cidOrUrl: string): string {
  if (cidOrUrl.startsWith("https://")) return cidOrUrl;
  return `https://cdn.bsky.app/img/avatar/plain/${did}/${cidOrUrl}`;
}

export function extractAvatarCid(url: string): string {
  const match = url.match(/\/([^/]+?)(?:@[a-z]+)?$/);
  return match?.[1] ?? '';
}

/** fetch profile directly from an actor's PDS — fallback for banned/suspended accounts */
export async function fetchProfileFromPds(did: string, pds: string): Promise<any | null> {
  try {
    const res = await fetch(
      `${pds}/xrpc/com.atproto.repo.getRecord?repo=${encodeURIComponent(did)}&collection=app.bsky.actor.profile&rkey=self`
    );
    if (!res.ok) return null;
    const data: any = await res.json();
    const val = data?.value;
    if (!val) return null;

    let avatar = '';
    const cid = val.avatar?.ref?.$link || val.avatar?.ref?.['$link'];
    if (cid) {
      avatar = `${pds}/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(did)}&cid=${encodeURIComponent(cid)}`;
    }

    return {
      did,
      handle: '',
      displayName: val.displayName || '',
      avatar,
      labels: [],
      createdAt: val.createdAt || '',
      associated: {},
    };
  } catch {
    return null;
  }
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

/** extract the fields we store from a bsky profile response (getProfiles/getProfile/typeahead) */
export interface ProfileFields {
  handle: string;
  displayName: string;
  avatarCid: string;
  labels: string;
  hidden: number;
  createdAt: string;
  associated: string;
}

export function extractProfileFields(profile: any, override?: 'show' | 'hide' | null): ProfileFields {
  let hidden = shouldHide(profile.labels) ? 1 : 0;
  if (override === 'show') hidden = 0;
  if (override === 'hide') hidden = 1;
  return {
    handle: profile.handle || '',
    displayName: profile.displayName || '',
    avatarCid: extractAvatarCid(profile.avatar || ''),
    labels: JSON.stringify(profile.labels || []),
    hidden,
    createdAt: profile.createdAt || '',
    associated: JSON.stringify(cleanAssociated(profile.associated)),
  };
}

/** strip anything that could break FTS5 syntax, preserving unicode letters/digits */
export function sanitize(q: string): string {
  return q.replace(/[^\p{L}\p{N}\s.-]/gu, "").trim();
}

export function html(body: string, extra?: Record<string, string> | number, status = 200): Response {
  if (typeof extra === "number") { status = extra; extra = undefined; }
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS, ...extra },
  });
}

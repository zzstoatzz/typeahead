import { CORS_HEADERS } from "./types";

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
  return `https://cdn.bsky.app/img/avatar/plain/${did}/${cidOrUrl}@jpeg`;
}

export function extractAvatarCid(url: string): string {
  const match = url.match(/\/([^/]+)@jpeg$/);
  return match?.[1] ?? '';
}

/** strip anything that could break FTS5 syntax, preserving unicode letters/digits */
export function sanitize(q: string): string {
  return q.replace(/[^\p{L}\p{N}\s.-]/gu, "").trim();
}

export function html(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS },
  });
}

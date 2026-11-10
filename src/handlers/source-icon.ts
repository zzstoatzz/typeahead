import type { SqlDb } from "../db";
import { brandFor } from "../pages/source-brands";

const MAX_BYTES = 256 * 1024;
const FETCH_TIMEOUT_MS = 4000;
const FOUND_TTL = 86_400;
const MISSING_TTL = 3_600;

/** Icons are served from our origin so the stats page can read their pixels
 *  (a cross-origin favicon taints the canvas). Only sources already in
 *  traffic_sources or the brand list are fetched, only over https, only
 *  image responses under MAX_BYTES, and a served SVG cannot run script. */
export function iconCandidates(source: string): string[] {
  const brand = brandFor(source);
  const host = /^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i.test(source)
    && !/\.(?:localhost|local|internal|test|invalid|ts\.net)$/i.test(source)
    && !/^[0-9.]+$/.test(source)
    ? source.toLowerCase() : null;
  return [brand?.icon, host ? `https://${host}/favicon.ico` : undefined].filter((u): u is string => u !== undefined);
}

function imageType(response: Response, url: string): string | null {
  const type = (response.headers.get("content-type") ?? "").split(";")[0]!.trim().toLowerCase();
  if (type.startsWith("image/")) return type;
  // plenty of servers send .ico as octet-stream
  if (url.endsWith(".ico") && (type === "" || type === "application/octet-stream")) return "image/x-icon";
  return null;
}

async function fetchIcon(url: string): Promise<{ body: ArrayBuffer; type: string } | null> {
  try {
    const response = await fetch(url, { redirect: "follow", signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), headers: { Accept: "image/*" } });
    if (!response.ok || !response.url.startsWith("https://")) return null;
    const type = imageType(response, url);
    if (!type || Number(response.headers.get("content-length") ?? 0) > MAX_BYTES) return null;
    const body = await response.arrayBuffer();
    return body.byteLength > 0 && body.byteLength <= MAX_BYTES ? { body, type } : null;
  } catch {
    return null;
  }
}

async function knownSource(db: SqlDb, source: string): Promise<boolean> {
  if (brandFor(source)) return true;
  return (await db.prepare("SELECT 1 AS ok FROM traffic_sources WHERE domain = ?1").bind(source).first<{ ok: number }>()) !== null;
}

export async function handleSourceIcon(request: Request, db: SqlDb, ctx: ExecutionContext): Promise<Response> {
  const source = new URL(request.url).searchParams.get("d") ?? "";
  const cache = caches.default;
  const cached = await cache.match(request);
  if (cached) return cached;

  const candidates = source.length <= 253 ? iconCandidates(source) : [];
  let icon: { body: ArrayBuffer; type: string } | null = null;
  if (candidates.length > 0 && await knownSource(db, source)) {
    for (const url of candidates) {
      icon = await fetchIcon(url);
      if (icon) break;
    }
  }
  const response = icon
    ? new Response(icon.body, { headers: { "Content-Type": icon.type, "Cache-Control": `public, max-age=${FOUND_TTL}` } })
    : new Response(null, { status: 404, headers: { "Cache-Control": `public, max-age=${MISSING_TTL}` } });
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox");
  ctx.waitUntil(cache.put(request, response.clone()));
  return response;
}

import type { SqlDb } from "./db";

/** fire-and-forget: increment 5-min search count + accumulate response time */
export async function recordMetric(db: SqlDb, ms: number, now = Date.now()): Promise<void> {
  const bucket = Math.floor(now / 300_000);
  await db.prepare(
    `INSERT INTO metrics (hour, searches, total_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(hour) DO UPDATE SET
       searches = searches + 1,
       total_ms = total_ms + ?2`
  )
    .bind(bucket, ms)
    .run();
}

export async function recordCacheHit(db: SqlDb, ms: number, now = Date.now()): Promise<void> {
  const bucket = Math.floor(now / 300_000);
  await db.prepare(
    `INSERT INTO metrics (hour, cache_hits, cache_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(hour) DO UPDATE SET
       cache_hits = cache_hits + 1,
       cache_ms = cache_ms + ?2`
  )
    .bind(bucket, ms)
    .run();
}

/** which app a request came from: X-Client > Origin > Referer (self → homepage) > unknown.
 *  X-Client is a raw request header — arbitrary bytes, any length, chosen by
 *  the caller. It gets stored, then rendered on /stats. The renderer escapes
 *  it, but this is the layer that decides what may enter the corpus at all:
 *  anything that is not hostname-shaped is not a traffic source. */
export function trafficDomain(request: Request): string {
  const client = request.headers.get("X-Client");
  const origin = request.headers.get("Origin");
  const referer = request.headers.get("Referer");
  const selfHost = new URL(request.url).hostname;

  let domain: string;
  if (client) {
    domain = client;
  } else if (origin) {
    try { domain = new URL(origin).hostname; } catch { domain = "unknown"; }
  } else if (referer) {
    try {
      const refHost = new URL(referer).hostname;
      domain = refHost === selfHost ? "homepage" : refHost;
    } catch { domain = "unknown"; }
  } else {
    domain = "unknown";
  }

  // normalize local/dev traffic
  if (domain === "localhost" || domain.startsWith("127.") || domain === "[::1]") {
    domain = "unknown";
  }
  if (!/^[a-zA-Z0-9._:[\]-]{1,64}$/.test(domain)) {
    domain = "unknown";
  }
  return domain;
}

/** fire-and-forget: increment cumulative hit counter per client identity */
export async function recordTrafficSource(db: SqlDb, request: Request, now = Date.now()): Promise<void> {
  const domain = trafficDomain(request);
  const bucket = Math.floor(now / 300_000);
  await db.batch([
    db.prepare(
      `INSERT INTO traffic_sources (domain, hits)
       VALUES (?1, 1)
       ON CONFLICT(domain) DO UPDATE SET hits = hits + 1`
    ).bind(domain),
    db.prepare(
      `INSERT INTO traffic_buckets (bucket, domain, hits)
       VALUES (?1, ?2, 1)
       ON CONFLICT(bucket, domain) DO UPDATE SET hits = hits + 1`
    ).bind(bucket, domain),
  ]);
}

/** the label a request is counted under in request_buckets. Coarse on purpose:
 *  the set must stay small and fixed so bucket × domain × endpoint × status is
 *  bounded by configuration, not by what callers send. Unknown paths all land
 *  in "other" — a 404 probe must never mint a new endpoint. */
export function endpointLabel(pathname: string): string {
  if (pathname === "/xrpc/tech.waow.typeahead.searchActors" ||
      pathname === "/xrpc/app.bsky.actor.searchActorsTypeahead") return "search";
  if (pathname === "/xrpc/app.bsky.actor.getProfiles" ||
      pathname === "/xrpc/app.bsky.actor.getProfile") return "getProfiles";
  if (pathname === "/request-indexing") return "request-indexing";
  if (pathname === "/source-icon") return "source-icon";
  if (pathname === "/admin/ingest") return "admin/ingest";
  if (pathname === "/admin/delete") return "admin/delete";
  if (pathname.startsWith("/admin/identity/")) return "admin/identity";
  if (pathname === "/admin/labels" || pathname.startsWith("/admin/labels/")) return "admin/labels";
  if (pathname.startsWith("/admin/")) return "admin";
  if (pathname.startsWith("/slot/control/")) return "slot-control";
  if (pathname.startsWith("/health/")) return "health";
  if (pathname.startsWith("/debug/")) return "debug";
  if (pathname === "/" || pathname === "/stats" || pathname === "/docs" ||
      pathname === "/reputation" || pathname === "/llms.txt" ||
      pathname === "/og-image" || pathname === "/.well-known/atproto-did" ||
      pathname === "/favicon.svg" || pathname === "/apple-touch-icon.png" ||
      pathname === "/site.webmanifest") return "page";
  return "other";
}

/** traffic_sources rows that are apps, not us: excludes internal probes
 *  (bench, cf-migration-*, migration-*), dev tunnels, and rfc1918 / loopback
 *  IPs. Shared by the /stats list and the hourly materialized count so the
 *  page and the link-preview card can never disagree about what an app is. */
export const TRAFFIC_SOURCE_WHERE = `WHERE domain != 'bench'
       AND domain NOT IN ('smoke-testing', 'community-smoke-test',
                          'cf-migration-verify', 'cf-migration-baseline',
                          'migration-check', 'migration-debug')
       AND domain NOT LIKE '%.trycloudflare.com'
       AND domain NOT LIKE '%.ngrok.app'
       AND domain NOT LIKE '%.ngrok.io'
       AND domain NOT LIKE '%.ngrok-free.app'
       AND domain NOT LIKE '192.168.%'
       AND domain NOT LIKE '10.%.%.%'
       AND domain NOT LIKE '127.%'`;

/** fire-and-forget: one row per 5-min bucket × app × endpoint × status, for
 *  EVERY request the worker answers (not just search cache misses, which is
 *  all traffic_buckets sees). This is the only place non-2xx responses are
 *  counted anywhere we own. */
export async function recordRequest(
  db: SqlDb,
  domain: string,
  endpoint: string,
  status: number,
  now = Date.now(),
): Promise<void> {
  const bucket = Math.floor(now / 300_000);
  await db.prepare(
    `INSERT INTO request_buckets (bucket, domain, endpoint, status, hits)
     VALUES (?1, ?2, ?3, ?4, 1)
     ON CONFLICT(bucket, domain, endpoint, status) DO UPDATE SET hits = hits + 1`
  )
    .bind(bucket, domain, endpoint, status)
    .run();
}

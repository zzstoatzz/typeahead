export interface Env {
  KV: KVNamespace;
  ADMIN_SECRET: string;
  RATE_LIMITER: RateLimit;
  RATE_LIMITER_STRICT: RateLimit;
  TURSO_URL: string;
  TURSO_AUTH_TOKEN: string;
  SEARCH_BACKEND_URL?: string;
  SEARCH_TRANSPORT?: Fetcher;
  SEARCH_CACHE_GENERATION?: string;
  PDS_SEARCH_DISABLED?: string;
  PDS_UI_ENABLED?: string;
  /// "1" re-asks Turso when the engine answers empty (limit ≤ 25 only).
  /// Off by default since 2026-09-04: see tursoFallbackOnEmpty.
  TURSO_FALLBACK_ON_EMPTY?: string;
  /// "1" sends the query to Turso when the engine is unreachable or errors.
  /// Off by default since 2026-09-04: see tursoFallbackOnUnreachable.
  TURSO_FALLBACK_ON_UNREACHABLE?: string;
  /// DID of the service's own atproto account (@typeahead.waow.tech).
  /// served at /.well-known/atproto-did for handle verification; the
  /// _lexicon DNS record points here so the service owns its lexicons.
  SERVICE_DID?: string;
  /// Emergency kill switch: when set (any non-empty value), the search
  /// handler bypasses everything (ingester, Turso, cache) and proxies
  /// directly to Bluesky's public typeahead API. Flip via:
  ///   bunx wrangler secret put EMERGENCY_PROXY_BSKY
  /// Turn off with:
  ///   bunx wrangler secret delete EMERGENCY_PROXY_BSKY
  EMERGENCY_PROXY_BSKY?: string;
  /// Discord-compatible webhook. When set, the hourly cron posts here on a
  /// freshness STATE CHANGE (ok -> stale, stale -> ok). Set via:
  ///   bunx wrangler secret put ALERT_WEBHOOK_URL
  /// Independent of Logfire on purpose: the 48h wedge on 2026-07-28 went
  /// unnoticed partly because the search service exported no telemetry at all.
  ALERT_WEBHOOK_URL?: string;
}

/** KV `stats_data`, written hourly by materializeStats */
export interface StatsKvData {
  metrics: { hour: number; searches: number; total_ms: number; cache_hits: number; cache_ms: number }[];
  snapshots: { hour: number; total: number; with_handles: number; with_avatars: number; hidden: number }[];
  deltas: { bucket: number; actors_delta: number; handles_delta: number; avatars_delta: number }[];
  traffic_series?: { hour: number; domain: string; hits: number }[];
  traffic_series_5m?: { bucket: number; domain: string; hits: number }[];
  traffic_daily?: { day: number; domain: string; hits: number }[];
  /** apps that have ever sent a search: traffic_sources rows minus probes,
   *  tunnels, private IPs, and the unknown/homepage attribution buckets */
  traffic_sources_total?: number;
  materialized_at: number;
}

/** KV `request_data`, written hourly by materializeRequests */
export interface RequestKvData {
  series?: { hour: number; domain: string; endpoint: string; status: number; hits: number }[];
  series_5m?: { bucket: number; domain: string; endpoint: string; status: number; hits: number }[];
  daily?: { day: number; domain: string; endpoint: string; status: number; hits: number }[];
  materialized_at: number;
}

export interface ActorRow {
  did: string;
  handle: string;
  display_name: string;
  avatar_url: string;
  labels: string;
  created_at: string;
  associated: string;
  pds: string;
}

/** getProfiles row: ActorRow plus the Turso-only profile-card columns. */
export interface ProfileRow extends ActorRow {
  description: string;
  banner_url: string;
  followers_count: number;
  follows_count: number;
  posts_count: number;
  profile_checked_at: number;
  updated_at: number;
}

export interface IngestEvent {
  did: string;
  handle?: string;
  handle_verified?: boolean;
  display_name?: string;
  avatar_cid?: string;
  description?: string;
  banner_cid?: string;
  /** resolved alongside handle by the ingester's slingshot pool — carrying it
   *  here is what retired the enrichment cron's per-DID pds backfill. */
  pds?: string;
}

export interface SlingshotResponse {
  did: string;
  handle: string;
  pds: string;
}

export const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client",
};

/** microcosm.blue etiquette: identify the project + contact on api requests */
export const MICROCOSM_UA = "typeahead (zzstoatzz.io)";

export const SLINGSHOT_URL =
  "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc";

export const BSKY_TYPEAHEAD_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead";

export const BSKY_GET_PROFILES_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles";

/** The mark: a lowercase t on the dark panel, with the accent block cursor
 *  the wordmark carries on every page. Served at /favicon.svg and rasterized
 *  for /apple-touch-icon.png; crawlers and home-screen installs cannot read a
 *  data: URI, which is why it is a route and not an inline href. */
export const FAVICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#101211"/><path d="M9 6h4v6h6v4h-6v7h6v4h-7q-3 0-3-3v-8H5v-4h4z" fill="#e8ece8"/><path d="M23 11h5v16h-5z" fill="#44aa99"/></svg>`;

export const FAVICON = `<link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
  <link rel="manifest" href="/site.webmanifest">`;

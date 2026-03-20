export interface Env {
  KV: KVNamespace;
  ADMIN_SECRET: string;
  RATE_LIMITER: RateLimit;
  RATE_LIMITER_STRICT: RateLimit;
  TURSO_URL: string;
  TURSO_AUTH_TOKEN: string;
}

export interface ActorRow {
  did: string;
  handle: string;
  display_name: string;
  avatar_url: string;
  labels: string;
  created_at: string;
  associated: string;
}

export interface IngestEvent {
  did: string;
  handle?: string;
  display_name?: string;
  avatar_cid?: string;
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

export const SLINGSHOT_URL =
  "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc";

export const BSKY_TYPEAHEAD_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead";

export const BSKY_GET_PROFILES_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles";

export const FAVICON = `<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='13' cy='13' r='9' fill='none' stroke='%2344aa99' stroke-width='2.5' opacity='0.8'/%3E%3Cline x1='20' y1='20' x2='28' y2='28' stroke='%2344aa99' stroke-width='2.5' stroke-linecap='round' opacity='0.8'/%3E%3C/svg%3E">`;

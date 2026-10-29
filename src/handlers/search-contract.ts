import { normalizePds } from "../pds";

export type Preference =
  | { mode: "pds_first"; host: string }
  | { mode: "boost"; host: string; boost: number; bits: string };

export interface SearchScope {
  pds: string | null;
  preference: Preference | null;
}

export function f32Bits(value: number): string {
  const bytes = new DataView(new ArrayBuffer(4));
  bytes.setFloat32(0, value === 0 ? 0 : value);
  return bytes.getUint32(0).toString(16).padStart(8, "0");
}

export function parseSearchScope(params: URLSearchParams): SearchScope {
  const pdsRaw = params.get("pds");
  const pds = pdsRaw === null ? null : normalizePds(pdsRaw);
  if (pdsRaw !== null && !pds) throw new Error("pds must be a DNS hostname or HTTP(S) origin without a port");
  const preferredRaw = params.get("prefer_pds");
  const modeRaw = params.get("preference");
  const boostRaw = params.get("boost");
  if (preferredRaw === null) {
    if (modeRaw !== null || boostRaw !== null) throw new Error("preference and boost require prefer_pds");
    return { pds, preference: null };
  }
  const host = normalizePds(preferredRaw);
  if (!host) throw new Error("prefer_pds must be a DNS hostname or HTTP(S) origin without a port");
  const mode = modeRaw ?? (boostRaw === null ? "pds_first" : "boost");
  if (mode === "pds_first") {
    if (boostRaw !== null) throw new Error("pds_first does not accept boost");
    return { pds, preference: { mode, host } };
  }
  if (mode !== "boost") throw new Error("preference must be pds_first or boost");
  if (boostRaw === null || !/^[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?$/.test(boostRaw)) throw new Error("boost requires a finite nonnegative f32 value");
  const value = Number(boostRaw);
  const rounded = Math.fround(value);
  if (!Number.isFinite(value) || !Number.isFinite(rounded) || value < 0) throw new Error("boost requires a finite nonnegative f32 value");
  const boost = rounded === 0 ? 0 : rounded;
  return { pds, preference: { mode, host, boost, bits: f32Bits(boost) } };
}

export function scopeParams(scope: SearchScope): URLSearchParams {
  const params = new URLSearchParams();
  if (scope.pds) params.set("pds", scope.pds);
  if (scope.preference) {
    params.set("prefer_pds", scope.preference.host);
    params.set("preference", scope.preference.mode);
    if (scope.preference.mode === "boost") params.set("boost", String(scope.preference.boost));
  }
  return params;
}

export function scopeAcknowledged(scope: SearchScope, headers: Headers): boolean {
  if (scope.pds && headers.get("X-Typeahead-Pds") !== scope.pds) return false;
  const preference = scope.preference;
  return !preference || (headers.get("X-Typeahead-Prefer-Pds") === preference.host
    && headers.get("X-Typeahead-Preference") === preference.mode
    && (preference.mode !== "boost" || headers.get("X-Typeahead-Boost") === preference.bits));
}

export function searchCacheGeneration(value?: string): string {
  const generation = value ?? "legacy";
  if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/.test(generation)) throw new Error("Invalid search cache generation");
  return generation;
}

export function searchCacheUrl(host: string, term: string, limit: number, scope: SearchScope, generation = "legacy"): string {
  const params = scopeParams(scope);
  return `https://${host}/__cache/search-v3/${searchCacheGeneration(generation)}/${encodeURIComponent(term)}:${limit}${params.size ? `?${params}` : ""}`;
}

export function validateBackendBody(text: string, limit: number): void {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || !("actors" in value) || !Array.isArray(value.actors) || value.actors.length > limit) throw new Error("Invalid search backend envelope");
  const dids = new Set<string>();
  for (const actor of value.actors) {
    if (!actor || typeof actor !== "object" || typeof actor.did !== "string" || !actor.did.startsWith("did:") || typeof actor.handle !== "string" || dids.has(actor.did)) throw new Error("Invalid search backend actor");
    dids.add(actor.did);
  }
}

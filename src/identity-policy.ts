import { z } from "zod";
import quarantine from "./identity-quarantine.json";

const quarantinedHandles = new Set(quarantine);

export function isSearchExcludedHandle(handle: string): boolean {
  const normalized = handle.toLowerCase();
  return normalized === "handle.invalid" || quarantinedHandles.has(normalized);
}

export function admittedHandle(event: { handle?: string; handle_verified?: boolean }): string {
  if (!event.handle) return "";
  if (event.handle === "handle.invalid") return event.handle;
  return event.handle_verified === true && !isSearchExcludedHandle(event.handle) ? event.handle : "";
}

const actorSchema = z.object({ did: z.string(), handle: z.string() }).passthrough();
const envelopeSchema = z.object({
  actors: z.array(actorSchema).optional(),
  profiles: z.array(actorSchema).optional(),
}).passthrough();

export function filterIdentityBody(body: string, collection: "actors" | "profiles"): string {
  const result = envelopeSchema.safeParse(JSON.parse(body));
  if (!result.success) return JSON.stringify({ [collection]: [] });
  const rows = result.data[collection];
  if (!rows) return body;
  const filtered = rows.filter((row) => !isSearchExcludedHandle(row.handle));
  if (filtered.length === rows.length) return body;
  return JSON.stringify({ ...result.data, [collection]: filtered });
}

export async function filterIdentityResponse(response: Response, collection: "actors" | "profiles"): Promise<Response> {
  if (!response.ok) return response;
  const headers = new Headers(response.headers);
  headers.delete("Content-Length");
  headers.delete("ETag");
  return new Response(filterIdentityBody(await response.text(), collection), { status: response.status, headers });
}

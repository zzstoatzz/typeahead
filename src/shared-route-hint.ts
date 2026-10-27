import * as z from "zod/mini";
import { routeSchema, type parseRoute, type routeHintRecord } from "./search-route";

export const HINT_CACHE = "typeahead-route-hints-v1";
export const HINT_KEY = "https://typeahead.waow.tech/__transport-route-hint/v1";
const hintSchema = z.object({ route: routeSchema, expires_at: z.number(), verified_until: z.optional(z.number()) });

export async function readSharedHintRecord(cache: Cache) {
  try {
    const response = await cache.match(HINT_KEY);
    if (!response?.body) return undefined;
    let bytes = 0;
    const bounded = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        bytes += chunk.byteLength;
        if (bytes > 16384) throw new Error("Route hint too large");
        controller.enqueue(chunk);
      },
    }));
    const hint = hintSchema.parse(await new Response(bounded).json());
    const now = Date.now();
    if (!Number.isSafeInteger(hint.expires_at) || hint.expires_at <= now || hint.expires_at > now + 60_000) return undefined;
    const verifiedUntil = hint.verified_until;
    return { route: hint.route, verified_until: verifiedUntil !== undefined && Number.isSafeInteger(verifiedUntil)
      && verifiedUntil > now && verifiedUntil <= now + 1000 && verifiedUntil <= hint.expires_at ? verifiedUntil : undefined };
  } catch {
    return undefined;
  }
}

export async function readSharedHint(cache: Cache) {
  return (await readSharedHintRecord(cache))?.route;
}

type Route = ReturnType<typeof parseRoute>;

export function reuseVerifiedRoute(current: Promise<Route>, known: Route | undefined, hint: Promise<Awaited<ReturnType<typeof readSharedHintRecord>>>) {
  if (!known) return current;
  return Promise.race([current, hint.then(record => {
    if (record?.verified_until !== undefined && Date.now() < record.verified_until
      && record.route.origin === known.origin && record.route.build_id === known.build_id) return known;
    return current;
  }, () => current)]);
}

export async function rememberSharedHint(cache: Cache, hint: ReturnType<typeof routeHintRecord>) {
  if (hint) await cache.put(HINT_KEY, Response.json(hint, { headers: { "Cache-Control": "public, max-age=60" } }));
}

export async function forgetSharedHint(cache: Cache) {
  await cache.delete(HINT_KEY);
}

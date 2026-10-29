import * as z from "zod/mini";

export const ROUTE_KEY = "control/search-routing.json";

export const routeSchema = z.pipe(z.object({
  version: z.literal(1),
  current: z.union([z.object({
    engine: z.optional(z.literal("native")),
    origin: z.enum([
      "https://typeahead-startup-20261024.fly.dev",
      "https://typeahead-search-refresh.fly.dev",
    ]),
    build_id: z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/)),
  }), z.object({
    engine: z.literal("tantivy"),
    origin: z.enum([
      "https://typeahead-tantivy-dev.fly.dev",
      "https://typeahead-tantivy-a.fly.dev",
      "https://typeahead-tantivy-b.fly.dev",
    ]),
    build_id: z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/)),
    format: z.literal("tantivy-0.27-f32-did-expiry-v4"),
    admission_sha256: z.string().check(z.regex(/^[a-f0-9]{64}$/)),
    binary_sha256: z.string().check(z.regex(/^[a-f0-9]{64}$/)),
    retrieval_strategy: z.enum(["per-segment", "global-merge"]),
  })]),
}), z.transform(({ current }) => current));

export const parseRoute = routeSchema.parse;

const retainedNativeSchema = z.looseObject({
  engine: z.optional(z.literal("native")),
  origin: z.enum(["https://typeahead-startup-20261024.fly.dev", "https://typeahead-search-refresh.fly.dev"]),
  build_id: z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/)),
  source_watermark: z.optional(z.int().check(z.gte(0))),
  machine_id: z.optional(z.string().check(z.regex(/^[a-f0-9]{14}$/))),
  volume_id: z.optional(z.string().check(z.regex(/^vol_[A-Za-z0-9]+$/))),
  legacy_build_id: z.optional(z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/))),
});

export function routeIdentity(route: ReturnType<typeof parseRoute>): string {
  return JSON.stringify(route.engine === "tantivy"
    ? [route.engine, route.origin, route.build_id, route.format, route.admission_sha256, route.binary_sha256, route.retrieval_strategy]
    : ["native", route.origin, route.build_id]);
}

export function responseMatchesRoute(route: ReturnType<typeof parseRoute>, response: Response): boolean {
  return route.engine !== "tantivy" || !response.ok || (
    response.headers.get("X-Typeahead-Engine") === "tantivy"
    && response.headers.get("X-Typeahead-Build") === route.build_id
    && response.headers.get("X-Typeahead-Format") === route.format
    && response.headers.get("X-Typeahead-Binary-Sha256") === route.binary_sha256
    && response.headers.get("X-Typeahead-Retrieval-Strategy") === route.retrieval_strategy
  );
}

export async function conditionalRouteUpdate(bucket: R2Bucket, expectedEtag: string | null, candidate: unknown): Promise<boolean> {
  const parsedCurrent = parseRoute({ version: 1, current: candidate });
  const current = parsedCurrent.engine === "tantivy" ? parsedCurrent : retainedNativeSchema.parse(candidate);
  const before = await bucket.get(ROUTE_KEY);
  if ((before?.etag ?? null) !== expectedEtag) return false;
  const envelope = before ? await before.json() : null;
  const retained = envelope === null ? undefined : z.object({
    current: z.unknown(),
    previous: z.optional(z.unknown()),
    native_rollback: z.optional(z.unknown()),
  }).parse(envelope);
  const parsedPrevious = before ? parseRoute(envelope) : null;
  const previous = parsedPrevious?.engine === "tantivy" ? parsedPrevious : retained ? retainedNativeSchema.parse(retained.current) : null;
  let anchor = retained?.native_rollback == null ? null : retainedNativeSchema.parse(retained.native_rollback);
  if (current.engine === "tantivy") {
    if (previous && previous.engine !== "tantivy") anchor = previous;
    if (!anchor && retained?.previous != null) {
      const earlier = parseRoute({ version: 1, current: retained.previous });
      if (earlier.engine !== "tantivy") anchor = retainedNativeSchema.parse(retained.previous);
    }
    if (!anchor) throw new Error("Tantivy promotion requires a retained native rollback anchor");
  }
  const options = expectedEtag === null
    ? { onlyIf: { etagDoesNotMatch: "*" } }
    : { onlyIf: { etagMatches: expectedEtag } };
  const result = await bucket.put(ROUTE_KEY, JSON.stringify({ version: 1, current, previous, ...(anchor ? { native_rollback: anchor } : {}) }), options);
  return result !== null;
}

export function allowedRoute(request: Request) {
  const path = new URL(request.url).pathname;
  if (request.method === "GET") {
    return ["/search", "/health", "/ready", "/index/status", "/debug/search", "/debug/index", "/route"].includes(path);
  }
  return request.method === "POST" && path === "/admin/refresh";
}

let cachedRoute: { route: ReturnType<typeof parseRoute>; expiresAt: number; hintExpiresAt: number } | undefined;
let sharedHintsEnabled = true;

export function sharedHintsAllowed() {
  return sharedHintsEnabled;
}

export function routeHintRecord() {
  if (cachedRoute && Date.now() < cachedRoute.hintExpiresAt) {
    return { route: { version: 1 as const, current: cachedRoute.route }, expires_at: cachedRoute.hintExpiresAt };
  }
  return undefined;
}

export function speculativeRoute() {
  if (cachedRoute && Date.now() < cachedRoute.hintExpiresAt) return cachedRoute.route;
  return undefined;
}

export async function readRoute(bucket: R2Bucket, cache: boolean) {
  if (cache && cachedRoute && Date.now() < cachedRoute.expiresAt) return cachedRoute.route;
  const before = cachedRoute;
  try {
    const object = await bucket.get(ROUTE_KEY);
    if (object === null || object.size > 16384) throw new Error("Missing search route");
    const route = parseRoute(await object.json());
    const validatedAt = Date.now();
    cachedRoute = { route, expiresAt: validatedAt + 1000, hintExpiresAt: validatedAt + 60_000 };
    sharedHintsEnabled = true;
    return route;
  } catch (error) {
    if (cachedRoute === before) {
      if (cachedRoute) cachedRoute.hintExpiresAt = 0;
      sharedHintsEnabled = false;
    }
    throw error;
  }
}

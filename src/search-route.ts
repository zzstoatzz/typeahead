import * as z from "zod/mini";

export const ROUTE_KEY = "control/search-routing.json";

const targetSchema = z.object({
  engine: z.literal("tantivy"),
  origin: z.enum([
    "https://typeahead-shadow-a.fly.dev",
    "https://typeahead-shadow-b.fly.dev",
  ]),
  build_id: z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/)),
  format: z.literal("tantivy-0.27-f32-did-expiry-v4"),
  admission_sha256: z.string().check(z.regex(/^[a-f0-9]{64}$/)),
  binary_sha256: z.string().check(z.regex(/^[a-f0-9]{64}$/)),
  retrieval_strategy: z.enum(["per-segment", "global-merge"]),
});

export const routeSchema = z.pipe(z.object({
  version: z.literal(1),
  current: targetSchema,
}), z.transform(({ current }) => current));

export const parseRoute = routeSchema.parse;

export type SearchRoute = ReturnType<typeof parseRoute>;

export function routeIdentity(route: SearchRoute): string {
  return JSON.stringify([route.engine, route.origin, route.build_id, route.format, route.admission_sha256, route.binary_sha256, route.retrieval_strategy]);
}

export function responseMatchesRoute(route: SearchRoute, response: Response): boolean {
  return !response.ok || (
    response.headers.get("X-Typeahead-Engine") === "tantivy"
    && response.headers.get("X-Typeahead-Build") === route.build_id
    && response.headers.get("X-Typeahead-Format") === route.format
    && response.headers.get("X-Typeahead-Binary-Sha256") === route.binary_sha256
    && response.headers.get("X-Typeahead-Retrieval-Strategy") === route.retrieval_strategy
  );
}

export async function conditionalRouteUpdate(bucket: R2Bucket, expectedEtag: string | null, candidate: z.input<typeof targetSchema>): Promise<boolean> {
  const current = targetSchema.parse(candidate);
  const before = await bucket.get(ROUTE_KEY);
  if ((before?.etag ?? null) !== expectedEtag) return false;
  const retained = before === null ? undefined : z.object({ current: z.unknown() }).parse(await before.json());
  const previous = targetSchema.safeParse(retained?.current);
  const options = expectedEtag === null
    ? { onlyIf: { etagDoesNotMatch: "*" } }
    : { onlyIf: { etagMatches: expectedEtag } };
  const next = { version: 1, current, previous: previous.success ? previous.data : undefined };
  const result = await bucket.put(ROUTE_KEY, JSON.stringify(next), options);
  return result !== null;
}

export function allowedRoute(request: Request) {
  const path = new URL(request.url).pathname;
  return request.method === "GET"
    && ["/search", "/health", "/ready", "/index/status", "/debug/search", "/actors", "/corpus", "/route"].includes(path);
}

let cachedRoute: { route: SearchRoute; expiresAt: number; hintExpiresAt: number } | undefined;
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

/** a source of routes already validated against R2, such as RouteAuthority */
export type RouteSource = { current(): Promise<{ route: SearchRoute; validatedAt: number }> };

type RouteBucket = { get(key: string): Promise<Pick<R2ObjectBody, "size" | "json"> | null> };

async function readBucketRoute(bucket: RouteBucket) {
  const object = await bucket.get(ROUTE_KEY);
  if (object === null || object.size > 16384) throw new Error("Missing search route");
  const route = parseRoute(await object.json());
  return { route, validatedAt: Date.now() };
}

/** After this, race a direct R2 read against the still-pending shared copy. */
export const SHARED_ROUTE_WAIT_MS = 50;

export async function readRoute(bucket: RouteBucket, cache: boolean, source?: RouteSource, ctx?: Pick<ExecutionContext, "waitUntil">) {
  if (cache && cachedRoute && Date.now() < cachedRoute.expiresAt) return cachedRoute.route;
  const before = cachedRoute;
  let sharedRead: ReturnType<typeof readBucketRoute> | undefined;
  let bucketRead: ReturnType<typeof readBucketRoute> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    let selected;
    if (cache && source) {
      sharedRead = Promise.resolve().then(() => source.current()).then(({ route, validatedAt: claimed }) => {
        const validatedAt = Math.min(claimed, Date.now());
        if (Date.now() - validatedAt >= 1000) throw new Error("Shared route expired");
        return { route: parseRoute({ version: 1, current: route }), validatedAt };
      });
      const fallback = new Promise<Awaited<ReturnType<typeof readBucketRoute>>>((resolve) => {
        const begin = () => {
          if (bucketRead) return;
          clearTimeout(timer);
          bucketRead = readBucketRoute(bucket);
          resolve(bucketRead);
        };
        timer = setTimeout(begin, SHARED_ROUTE_WAIT_MS);
        sharedRead?.catch(begin);
      });
      selected = await Promise.race([sharedRead.catch(() => fallback), fallback]);
    } else {
      selected = await readBucketRoute(bucket);
    }
    const { route, validatedAt } = selected;
    cachedRoute = { route, expiresAt: validatedAt + 1000, hintExpiresAt: validatedAt + 60_000 };
    sharedHintsEnabled = true;
    return route;
  } catch (error) {
    if (cachedRoute === before) {
      if (cachedRoute) cachedRoute.hintExpiresAt = 0;
      sharedHintsEnabled = false;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (sharedRead) ctx?.waitUntil(sharedRead.catch(() => undefined));
    if (bucketRead) ctx?.waitUntil(bucketRead.catch(() => undefined));
  }
}

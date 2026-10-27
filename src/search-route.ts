import * as z from "zod/mini";

export const ROUTE_KEY = "control/search-routing.json";

export const routeSchema = z.pipe(z.object({
  version: z.literal(1),
  current: z.object({
    origin: z.enum([
      "https://typeahead-startup-20261024.fly.dev",
      "https://typeahead-search-refresh.fly.dev",
    ]),
    build_id: z.string().check(z.regex(/^b[0-9]{10}-[A-Za-z0-9_-]+$/)),
  }),
}), z.transform(({ current }) => current));

export const parseRoute = routeSchema.parse;

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
    return { route: { version: 1 as const, current: cachedRoute.route }, expires_at: cachedRoute.hintExpiresAt, verified_until: cachedRoute.expiresAt };
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

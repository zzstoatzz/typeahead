import * as z from "zod/mini";

export const ROUTE_KEY = "control/search-routing.json";

const routeSchema = z.pipe(z.object({
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

let cachedRoute: { route: ReturnType<typeof parseRoute>; expiresAt: number } | undefined;

export async function readRoute(bucket: R2Bucket, cache: boolean) {
  if (cache && cachedRoute && Date.now() < cachedRoute.expiresAt) return cachedRoute.route;
  const object = await bucket.get(ROUTE_KEY);
  if (object === null || object.size > 16384) throw new Error("Missing search route");
  const route = parseRoute(await object.json());
  cachedRoute = { route, expiresAt: Date.now() + 1000 };
  return route;
}

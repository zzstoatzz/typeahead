export const ROUTE_KEY = "control/search-routing.json";

const backends = new Set([
  "https://typeahead-startup-20261024.fly.dev",
  "https://typeahead-search-refresh.fly.dev",
]);

export function parseRoute(value: unknown) {
  if (typeof value !== "object" || value === null || !("version" in value) || value.version !== 1 ||
      !("current" in value) || typeof value.current !== "object" || value.current === null) {
    throw new Error("Invalid search route");
  }
  const current = value.current;
  if (!("origin" in current) || typeof current.origin !== "string" || !backends.has(current.origin) ||
      !("build_id" in current) || typeof current.build_id !== "string" ||
      !/^b[0-9]{10}-[A-Za-z0-9_-]+$/.test(current.build_id)) {
    throw new Error("Invalid search route target");
  }
  return { origin: current.origin, build_id: current.build_id };
}

export function allowedRoute(request: Request) {
  const path = new URL(request.url).pathname;
  if (request.method === "GET") {
    return ["/search", "/health", "/ready", "/index/status", "/debug/search", "/debug/index", "/route"].includes(path);
  }
  return request.method === "POST" && path === "/admin/refresh";
}

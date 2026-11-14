import { afterAll, beforeEach, test, expect, mock, spyOn } from "bun:test";
import { GENERATION, controlStore } from "./control-d1-fixture";
import type { SqlDb } from "./db";
import { statsStores } from "./stats-d1-fixture";
import type { Env } from "./types";

// The request recorder wraps the router: every answer is counted under its
// real status, and an UNCAUGHT throw — what the edge reports as 1101/500 —
// is counted as 500 and rethrown unchanged. This is the regression guard for
// the 2026-08-23 finding that /admin/ingest 500'd 8–35% of the time for a
// day with nothing we own counting it.

const stores = await statsStores();
afterAll(() => stores.dispose());
beforeEach(async () => {
  await stores.db.batch(["request_buckets", "traffic_sources", "traffic_buckets", "metrics"].map((table) => stores.db.prepare(`DELETE FROM ${table}`)));
});

/** what the recorder counted, summed over the 5-minute buckets a test may straddle */
const recorded = async () => (await stores.db.prepare(
  "SELECT domain, endpoint, status, SUM(hits) AS hits FROM request_buckets GROUP BY domain, endpoint, status ORDER BY endpoint, status",
).all<{ domain: string; endpoint: string; status: number; hits: number }>()).results;

const statsHandles: SqlDb[] = [];
const realStats = await import("./handlers/stats");
mock.module("./handlers/stats", () => ({
  ...realStats,
  handleStats: async (_request: Request, db: SqlDb) => { statsHandles.push(db); throw new Error("boom"); },
}));

const worker = (await import("./index")).default;
type FakeKv = { get(key: string): Promise<string | null>; put(key: string, value: string): Promise<void> };
function fakeEnv(fields: Omit<Partial<Env>, "KV"> & { KV?: FakeKv }): Env {
  // SAFETY: the routes exercised here (404, OPTIONS, the throwing /stats, the
  // search route with a dead engine) read only the fields each test passes.
  // The cron and the routes use KV only through get(key) and put(key, value).
  return fields as Env;
}

// SAFETY: bun has no `caches` global; the search handler reads only
// `caches.default.match` and `.put`, which this stub provides.
const cacheHost = globalThis as { caches?: { default: { match(): Promise<undefined>; put(): Promise<void> } } };
const ENV = fakeEnv({ STATS_DB: stores.d1 });

// SAFETY: the scheduled handler never reads its event.
const scheduledEvent = {} as ScheduledEvent;

function ctx() {
  const tasks: Promise<unknown>[] = [];
  const c = { waitUntil: (p: Promise<unknown>) => { tasks.push(p); }, passThroughOnException() {} };
  // SAFETY: the worker's fetch handler uses the context only via waitUntil and
  // passThroughOnException; the runtime-owned members (exports, props, tracing)
  // are never read.
  return { ctx: c as ExecutionContext, tasks };
}

test("records the real status of a served response", async () => {
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/nope", { headers: { "X-Client": "plyr.fm" } }), ENV, c);
  expect(res.status).toBe(404);
  expect(tasks).toHaveLength(1);
  await Promise.all(tasks);
  expect(await recorded()).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404, hits: 1 }]);
});

test("the slot control routes answer from the control database and are counted under one label", async () => {
  const store = await controlStore();
  try {
    const secret = "slot-control-secret-of-enough-length";
    const env = fakeEnv({ ...ENV, CONTROL_DB: store.d1, SLOT_CONTROL_SECRET: secret, ADMIN_SECRET: "admin-secret" });
    const { ctx: c, tasks } = ctx();
    const headers = { Authorization: `Bearer ${secret}` };
    const policy = await worker.fetch(new Request("https://typeahead.waow.tech/slot/control/policy", { headers }), env, c);
    expect(policy.status).toBe(200);
    expect(JSON.parse(await policy.text())).toEqual({ generation: GENERATION, revision: 1, overrides: [], blocked_domains: [] });
    const requests = await worker.fetch(new Request("https://typeahead.waow.tech/slot/control/index-requests?after=0", { headers }), env, c);
    expect(JSON.parse(await requests.text())).toEqual({ generation: GENERATION, requests: [], more: false });
    const posted = await worker.fetch(new Request("https://typeahead.waow.tech/slot/control/policy", { method: "POST", headers }), env, c);
    expect(posted.status).toBe(404);
    const filled = await worker.fetch(new Request("https://typeahead.waow.tech/slot/control/profile-fill", { headers }), env, c);
    expect(filled.status).toBe(404);
    await Promise.all(tasks);
    expect(await recorded()).toEqual([
      { domain: "unknown", endpoint: "slot-control", status: 200, hits: 2 },
      { domain: "unknown", endpoint: "slot-control", status: 404, hits: 2 },
    ]);
  } finally {
    await store.dispose();
  }
});

test("an uncaught throw is counted as 500 and still propagates", async () => {
  const { ctx: c, tasks } = ctx();
  await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), ENV, c)).rejects.toThrow("boom");
  await Promise.all(tasks);
  expect(await recorded()).toEqual([{ domain: "unknown", endpoint: "page", status: 500, hits: 1 }]);
});

test("OPTIONS preflights are answered without being counted", async () => {
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/xrpc/x", { method: "OPTIONS" }), ENV, c);
  expect(res.status).toBe(204);
  await Promise.all(tasks);
  expect(await recorded()).toEqual([]);
});

// The engine is one machine and restarts in ~15 s. While it is down unscoped
// search falls back to Bluesky; with every fetch refused here, both providers
// fail and the answer is 503.
test("an unreachable engine and an unreachable Bluesky answer unavailable", async () => {
  const realFetch = globalThis.fetch;
  const realCaches = cacheHost.caches;
  const asked: string[] = [];
  const refuse = async (input: RequestInfo | URL) => { asked.push(new URL(String(input)).host); throw new Error("connect ECONNREFUSED"); };
  globalThis.fetch = Object.assign(refuse, { preconnect: realFetch.preconnect });
  cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };
  try {
    const env = fakeEnv({
      ...ENV,
      SEARCH_BACKEND_URL: "https://engine.invalid",
      RATE_LIMITER: { limit: async () => ({ success: true }) },
    });
    const { ctx: c, tasks } = ctx();
    const res = await worker.fetch(
      new Request("https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=8", { headers: { "X-Client": "plyr.fm" } }),
      env, c,
    );
    expect(res.status).toBe(503);
    expect(await res.json<unknown>()).toEqual({ error: "SearchUnavailable", message: "Search and emergency fallback are unavailable" });
    await Promise.all(tasks);
    expect(asked).toEqual(["engine.invalid", "public.api.bsky.app"]);
    expect(await recorded()).toEqual([{ domain: "plyr.fm", endpoint: "search", status: 503, hits: 1 }]);
  } finally {
    globalThis.fetch = realFetch;
    cacheHost.caches = realCaches;
  }
});

// A statistic that is only in the D1 fixture can come back only if the route
// read the statistics database.
test("/stats, /source-icon and the cron's materialization read the statistics database", async () => {
  const realFetch = globalThis.fetch;
  const realCaches = cacheHost.caches;
  const fetched: string[] = [];
  const refuse = async (input: RequestInfo | URL) => { fetched.push(String(input)); throw new Error("connect ECONNREFUSED"); };
  globalThis.fetch = Object.assign(refuse, { preconnect: realFetch.preconnect });
  cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };
  const kv = new Map<string, string>();
  const KV = { get: async (key: string) => kv.get(key) ?? null, put: async (key: string, value: string) => { kv.set(key, value); } };
  try {
    await stores.db.batch([
      stores.db.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-d1.example', 3)"),
      stores.db.prepare("INSERT INTO traffic_buckets (bucket, domain, hits) VALUES (?1, 'only-in-d1.example', 3)").bind(Math.floor(Date.now() / 300_000)),
    ]);
    const env = fakeEnv({ ...ENV, KV });

    const icon = ctx();
    const known = await worker.fetch(new Request("https://typeahead.waow.tech/source-icon?d=only-in-d1.example"), env, icon.ctx);
    expect(known.status).toBe(404);
    await Promise.all(icon.tasks);
    expect(fetched).toEqual(["https://only-in-d1.example/favicon.ico"]);

    fetched.length = 0;
    const unknown = ctx();
    await worker.fetch(new Request("https://typeahead.waow.tech/source-icon?d=never-seen.example"), env, unknown.ctx);
    await Promise.all(unknown.tasks);
    expect(fetched).toEqual([]);

    statsHandles.length = 0;
    const page = ctx();
    await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), env, page.ctx)).rejects.toThrow("boom");
    await Promise.all(page.tasks);
    expect((await statsHandles[0]!.prepare("SELECT domain, hits FROM traffic_sources").all()).results).toEqual([{ domain: "only-in-d1.example", hits: 3 }]);

    await worker.scheduled(scheduledEvent, env, ctx().ctx);
    expect(JSON.parse(kv.get("stats_data") ?? "null")?.traffic_series_5m).toMatchObject([{ domain: "only-in-d1.example", hits: 3 }]);
  } finally {
    globalThis.fetch = realFetch;
    cacheHost.caches = realCaches;
  }
});

test("without the statistics binding /stats and the two statistics cron steps fail, and the cron still finishes", async () => {
  const kv = new Map<string, string>();
  const KV = { get: async (key: string) => kv.get(key) ?? null, put: async (key: string, value: string) => { kv.set(key, value); } };
  const env = fakeEnv({ KV });
  const { ctx: c, tasks } = ctx();
  await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), env, c)).rejects.toThrow("this Worker has no STATS_DB binding");
  await Promise.all(tasks);

  const lines: string[] = [];
  const log = spyOn(console, "log").mockImplementation((line: string) => { lines.push(line); });
  try {
    await worker.scheduled(scheduledEvent, env, ctx().ctx);
  } finally {
    log.mockRestore();
  }
  const events = lines.map((line) => JSON.parse(line));
  expect(events.filter((event) => event.event === "cron_step_failed" && /no STATS_DB binding/.test(event.error)).map((event) => event.step))
    .toEqual(["materializeStats", "recordProfile"]);
  expect(Object.keys(events.find((event) => event.event === "cron_done").phases)).toEqual(
    ["freshness", "recordSnapshot", "recordExclusions", "materializeStats", "recordProfile", "controlHealth", "pruneIndexRequests"]);
  expect(kv.has("stats_data")).toBe(false);
});

// The endpoints that wrote the retired actor tables say they are gone,
// whatever the method. The environment here holds the statistics database
// and nothing else, so an answer proves no other store was reached for.
test("the endpoints that wrote the retired actor tables answer 410 and read nothing", async () => {
  const calls: [string, string][] = [
    ["POST", "/admin/ingest"], ["POST", "/admin/delete"], ["POST", "/admin/enrich-profiles"],
    ["POST", "/admin/identity/lease"], ["POST", "/admin/identity/complete"], ["GET", "/admin/identity/plc"],
    ["POST", "/admin/identity/plc"], ["GET", "/admin/identity/status"],
    ["POST", "/admin/labels"], ["POST", "/admin/labels/reconcile"], ["GET", "/admin/labels/cursor"],
  ];
  for (const [method, path] of calls) {
    const { ctx: c, tasks } = ctx();
    const res = await worker.fetch(new Request(`https://typeahead.waow.tech${path}`, { method }), ENV, c);
    expect([path, res.status]).toEqual([path, 410]);
    expect(await res.text()).toBe('{"error":"Gone","message":"this endpoint wrote the Turso actor tables, which are retired; store slots follow the network themselves"}');
    await Promise.all(tasks);
  }
  expect(await recorded()).toEqual([
    { domain: "unknown", endpoint: "admin", status: 410, hits: 1 },
    { domain: "unknown", endpoint: "admin/delete", status: 410, hits: 1 },
    { domain: "unknown", endpoint: "admin/identity", status: 410, hits: 5 },
    { domain: "unknown", endpoint: "admin/ingest", status: 410, hits: 1 },
    { domain: "unknown", endpoint: "admin/labels", status: 410, hits: 3 },
  ]);

  const { ctx: c, tasks } = ctx();
  const neighbour = await worker.fetch(new Request("https://typeahead.waow.tech/admin/labels/other", { method: "POST" }), ENV, c);
  expect(neighbour.status).toBe(404);
  await Promise.all(tasks);
});

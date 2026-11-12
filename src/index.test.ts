import { test, expect, mock } from "bun:test";
import type { InValue } from "@libsql/client/web";
import { GENERATION, controlStore } from "./control-d1-fixture";
import type { SqlDb } from "./db";
import { statsStores } from "./stats-d1-fixture";
import type { Env } from "./types";

// The request recorder wraps the router: every answer is counted under its
// real status, and an UNCAUGHT throw — what the edge reports as 1101/500 —
// is counted as 500 and rethrown unchanged. This is the regression guard for
// the 2026-08-23 finding that /admin/ingest 500'd 8–35% of the time for a
// day with nothing we own counting it.

const recorded: { domain: string; endpoint: string; status: number }[] = [];
const executed: string[] = [];
// bun's module mocks are process-wide: mock the libsql client factory (the
// lowest layer) rather than ./db, so db.test.ts still exercises the real getDb.
// Every client records request_buckets upserts; everything else is a no-op.
mock.module("@libsql/client/web", () => ({
  createClient: () => ({
    async execute({ sql, args }: { sql: string; args: InValue[] }) {
      executed.push(sql);
      if (/INSERT INTO request_buckets/.test(sql)) {
        const [, domain, endpoint, status] = args;
        recorded.push({ domain: String(domain), endpoint: String(endpoint), status: Number(status) });
      }
      return { rows: [], rowsAffected: 1 };
    },
    async batch(stmts: { sql?: string }[]) {
      for (const st of stmts) executed.push(String(st.sql ?? ""));
      return stmts.map(() => ({ rows: [], rowsAffected: 1 }));
    },
  }),
}));
const statsHandles: SqlDb[] = [];
const realStats = await import("./handlers/stats");
mock.module("./handlers/stats", () => ({
  ...realStats,
  handleStats: async (_request: Request, db: SqlDb) => { statsHandles.push(db); throw new Error("boom"); },
}));

const worker = (await import("./index")).default;
type FakeKv = { get(key: string): Promise<string | null>; put(key: string, value: string): Promise<void> };
function fakeEnv(fields: Omit<Partial<Env>, "KV"> & { KV?: FakeKv; TURSO_URL: string; TURSO_AUTH_TOKEN: string }): Env {
  // SAFETY: the routes exercised here (404, OPTIONS, the throwing /stats, the
  // search route with a dead engine) read only the fields each test passes,
  // and the libsql client is mocked above. The cron and the routes use KV only
  // through get(key) and put(key, value).
  return fields as Env;
}

// SAFETY: bun has no `caches` global; the search handler reads only
// `caches.default.match` and `.put`, which this stub provides.
const cacheHost = globalThis as { caches?: { default: { match(): Promise<undefined>; put(): Promise<void> } } };
const ENV = fakeEnv({ TURSO_URL: "https://example.turso.io", TURSO_AUTH_TOKEN: "t" });

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
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/nope", { headers: { "X-Client": "plyr.fm" } }), ENV, c);
  expect(res.status).toBe(404);
  await Promise.all(tasks);
  expect(recorded).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404 }]);
});

test("with dual write on, the same answer is also counted in D1", async () => {
  recorded.length = 0;
  const stores = await statsStores();
  try {
    const { ctx: c, tasks } = ctx();
    const env = fakeEnv({ ...ENV, STATS_DB: stores.d1, STATS_DUAL_WRITE: "1" });
    const res = await worker.fetch(new Request("https://typeahead.waow.tech/nope", { headers: { "X-Client": "plyr.fm" } }), env, c);
    expect(res.status).toBe(404);
    expect(tasks).toHaveLength(2);
    await Promise.all(tasks);
    expect(recorded).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404 }]);
    const { results } = await stores.mirror.prepare("SELECT domain, endpoint, status, hits FROM request_buckets").all();
    expect(results).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404, hits: 1 }]);
  } finally {
    await stores.dispose();
  }
});

test("the slot control routes answer from D1 alone and are counted under one label", async () => {
  recorded.length = 0;
  executed.length = 0;
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
    await Promise.all(tasks);
    expect(recorded).toEqual([
      { domain: "unknown", endpoint: "slot-control", status: 200 },
      { domain: "unknown", endpoint: "slot-control", status: 200 },
      { domain: "unknown", endpoint: "slot-control", status: 404 },
    ]);
    expect(executed.filter((sql) => !/request_buckets/.test(sql))).toEqual([]);
  } finally {
    await store.dispose();
  }
});

test("an uncaught throw is counted as 500 and still propagates", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), ENV, c)).rejects.toThrow("boom");
  await Promise.all(tasks);
  expect(recorded).toEqual([{ domain: "unknown", endpoint: "page", status: 500 }]);
});

test("OPTIONS preflights are answered without being counted", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/xrpc/x", { method: "OPTIONS" }), ENV, c);
  expect(res.status).toBe(204);
  await Promise.all(tasks);
  expect(recorded).toEqual([]);
});

// The engine is one machine and restarts in ~15 s. While it is down the
// worker must report unavailable without re-asking Turso for every query: on 2026-09-04
// one such restart under a viral burst put Turso over its connection limit
// and the ingester dropped events. Turso-on-unreachable is opt-in. Since
// 2026-10-31 unscoped search falls back to Bluesky; with every fetch refused
// here, both providers fail and the answer is 503.
test("an unreachable engine returns unavailable without touching Turso", async () => {
  recorded.length = 0;
  executed.length = 0;
  const realFetch = globalThis.fetch;
  const realCaches = cacheHost.caches;
  const refuse = async () => { throw new Error("connect ECONNREFUSED"); };
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
    expect(executed.filter((sql) => /FROM actors/i.test(sql))).toEqual([]);
  } finally {
    globalThis.fetch = realFetch;
    cacheHost.caches = realCaches;
  }
});

// The read switch (docs/plans/stats-off-turso.md, step 6). The libsql client
// above answers every query with no rows, so a statistic that is only in the
// D1 fixture can come back only if the route read D1.
test("STATS_READ_SOURCE=d1 hands /stats, /source-icon and the cron's materialization the D1 store", async () => {
  const stores = await statsStores();
  const realFetch = globalThis.fetch;
  const realCaches = cacheHost.caches;
  const fetched: string[] = [];
  const refuse = async (input: RequestInfo | URL) => { fetched.push(String(input)); throw new Error("connect ECONNREFUSED"); };
  globalThis.fetch = Object.assign(refuse, { preconnect: realFetch.preconnect });
  cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };
  const kv = new Map<string, string>();
  const KV = { get: async (key: string) => kv.get(key) ?? null, put: async (key: string, value: string) => { kv.set(key, value); } };
  try {
    await stores.mirror.batch([
      stores.mirror.prepare("INSERT INTO traffic_sources (domain, hits) VALUES ('only-in-d1.example', 3)"),
      stores.mirror.prepare("INSERT INTO traffic_buckets (bucket, domain, hits) VALUES (?1, 'only-in-d1.example', 3)").bind(Math.floor(Date.now() / 300_000)),
    ]);
    const known = async (env: Env) => {
      fetched.length = 0;
      executed.length = 0;
      const { ctx: c, tasks } = ctx();
      const res = await worker.fetch(new Request("https://typeahead.waow.tech/source-icon?d=only-in-d1.example"), env, c);
      expect(res.status).toBe(404);
      await Promise.all(tasks);
      return { fetched: [...fetched], turso: executed.filter((sql) => /traffic_sources WHERE domain/.test(sql)).length };
    };
    const readsD1 = async (env: Env) => {
      statsHandles.length = 0;
      const { ctx: c, tasks } = ctx();
      await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), env, c)).rejects.toThrow("boom");
      await Promise.all(tasks);
      return (await statsHandles[0]!.prepare("SELECT hits FROM traffic_sources").all()).results.length === 1;
    };
    const materialized = async (env: Env) => {
      kv.clear();
      await worker.scheduled(scheduledEvent, env, ctx().ctx);
      return JSON.parse(kv.get("stats_data") ?? "null")?.traffic_series_5m;
    };

    const base = { ...ENV, STATS_DB: stores.d1, STATS_DUAL_WRITE: "1", KV };
    const turso = fakeEnv({ ...base, STATS_READ_SOURCE: "turso" });
    const d1 = fakeEnv({ ...base, STATS_READ_SOURCE: "d1" });

    expect(await known(turso)).toEqual({ fetched: [], turso: 1 });
    expect(await known(d1)).toEqual({ fetched: ["https://only-in-d1.example/favicon.ico"], turso: 0 });
    expect(await readsD1(turso)).toBe(false);
    expect(await readsD1(d1)).toBe(true);
    expect(await materialized(turso)).toEqual([]);
    expect(await materialized(d1)).toMatchObject([{ domain: "only-in-d1.example", hits: 3 }]);
  } finally {
    globalThis.fetch = realFetch;
    cacheHost.caches = realCaches;
    await stores.dispose();
  }
});

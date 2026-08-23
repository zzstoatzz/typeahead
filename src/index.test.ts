import { test, expect, mock } from "bun:test";

// The request recorder wraps the router: every answer is counted under its
// real status, and an UNCAUGHT throw — what the edge reports as 1101/500 —
// is counted as 500 and rethrown unchanged. This is the regression guard for
// the 2026-08-23 finding that /admin/ingest 500'd 8–35% of the time for a
// day with nothing we own counting it.

const recorded: { domain: string; endpoint: string; status: number }[] = [];
const fakeDb = {
  prepare(sql: string) {
    const s: any = {
      bind(...a: any[]) { s.binds = a; return s; },
      async run() {
        if (/INSERT INTO request_buckets/.test(sql)) {
          const [, domain, endpoint, status] = s.binds;
          recorded.push({ domain, endpoint, status });
        }
        return { meta: { changes: 1 } };
      },
    };
    return s;
  },
};
// bun's module mocks are process-wide, so spread the real modules and override
// only what this file needs — other test files import these too.
const realDb = await import("./db");
const realStats = await import("./handlers/stats");
mock.module("./db", () => ({ ...realDb, getDb: async () => fakeDb }));
mock.module("./handlers/stats", () => ({
  ...realStats,
  handleStats: async () => { throw new Error("boom"); },
}));

const worker = (await import("./index")).default;

function ctx() {
  const tasks: Promise<unknown>[] = [];
  return { ctx: { waitUntil: (p: Promise<unknown>) => tasks.push(p), passThroughOnException() {} } as any, tasks };
}

test("records the real status of a served response", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/nope", { headers: { "X-Client": "plyr.fm" } }), {} as any, c);
  expect(res.status).toBe(404);
  await Promise.all(tasks);
  expect(recorded).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404 }]);
});

test("an uncaught throw is counted as 500 and still propagates", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  await expect(worker.fetch(new Request("https://typeahead.waow.tech/stats"), {} as any, c)).rejects.toThrow("boom");
  await Promise.all(tasks);
  expect(recorded).toEqual([{ domain: "unknown", endpoint: "page", status: 500 }]);
});

test("OPTIONS preflights are answered without being counted", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/xrpc/x", { method: "OPTIONS" }), {} as any, c);
  expect(res.status).toBe(204);
  await Promise.all(tasks);
  expect(recorded).toEqual([]);
});

import { test, expect, mock } from "bun:test";

// The request recorder wraps the router: every answer is counted under its
// real status, and an UNCAUGHT throw — what the edge reports as 1101/500 —
// is counted as 500 and rethrown unchanged. This is the regression guard for
// the 2026-08-23 finding that /admin/ingest 500'd 8–35% of the time for a
// day with nothing we own counting it.

const recorded: { domain: string; endpoint: string; status: number }[] = [];
// bun's module mocks are process-wide: mock the libsql client factory (the
// lowest layer) rather than ./db, so db.test.ts still exercises the real getDb.
// Every client records request_buckets upserts; everything else is a no-op.
mock.module("@libsql/client/web", () => ({
  createClient: () => ({
    async execute({ sql, args }: { sql: string; args: any[] }) {
      if (/INSERT INTO request_buckets/.test(sql)) {
        const [, domain, endpoint, status] = args;
        recorded.push({ domain, endpoint, status });
      }
      return { rows: [], rowsAffected: 1 };
    },
    async batch(stmts: any[]) { return stmts.map(() => ({ rows: [], rowsAffected: 1 })); },
  }),
}));
const realStats = await import("./handlers/stats");
mock.module("./handlers/stats", () => ({
  ...realStats,
  handleStats: async () => { throw new Error("boom"); },
}));

const worker = (await import("./index")).default;
const ENV: any = { TURSO_URL: "https://example.turso.io", TURSO_AUTH_TOKEN: "t" };

function ctx() {
  const tasks: Promise<unknown>[] = [];
  return { ctx: { waitUntil: (p: Promise<unknown>) => tasks.push(p), passThroughOnException() {} } as any, tasks };
}

test("records the real status of a served response", async () => {
  recorded.length = 0;
  const { ctx: c, tasks } = ctx();
  const res = await worker.fetch(new Request("https://typeahead.waow.tech/nope", { headers: { "X-Client": "plyr.fm" } }), ENV, c);
  expect(res.status).toBe(404);
  await Promise.all(tasks);
  expect(recorded).toEqual([{ domain: "plyr.fm", endpoint: "other", status: 404 }]);
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

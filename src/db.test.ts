import { test, expect } from "bun:test";
import { getDb } from "./db";

// @libsql/client puts a promise-limit(20) inside every client. Share one client
// across requests and that limiter is shared too: a slot stranded by a canceled
// request is gone for the isolate's life, and a request queued behind another
// request's I/O is what Cloudflare answers with 1101 "will never generate a
// response". Each call must get its own client so no request can wait on another.
test("getDb hands every caller its own client", async () => {
  const env: any = { TURSO_URL: "https://example.turso.io", TURSO_AUTH_TOKEN: "t" };
  const [a, b] = await Promise.all([getDb(env), getDb(env)]);
  expect(a).not.toBe(b);
});

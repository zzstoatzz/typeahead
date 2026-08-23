import { test, expect } from "bun:test";
import { handleIngest } from "./ingest";
import type { Env, IngestEvent } from "../types";
import type { SqlValue, Stmt, TursoDB } from "../db";

type Call = { sql: string; binds: SqlValue[] };
type FakeStmt = Stmt & { sql: string; binds: SqlValue[] };

function fakeEnv(fields: { ADMIN_SECRET: string }): Env {
  // SAFETY: handleIngest reads ADMIN_SECRET for the bearer check and nothing
  // else off Env when the secret matches, which every test here arranges.
  return fields as Env;
}

function fakeCtx() {
  const promises: Promise<unknown>[] = [];
  const ctx = { waitUntil: (p: Promise<unknown>) => { promises.push(p); } };
  // SAFETY: handleIngest touches the context only through waitUntil; the
  // runtime-owned members (exports, props, tracing) are never read.
  return { ctx: ctx as ExecutionContext, promises };
}

// Regression for the 2026-06-17 actors_total undercount: new actors first seen
// via a profile/identity event (handle-bearing) went through the upsert path and
// were NEVER counted — only bare-DID inserts were. The fix probes every event
// with INSERT OR IGNORE and counts changes===1, so ALL new actors are counted.

function mockReq(events: IngestEvent[]) {
  return new Request("https://x/admin/ingest", {
    method: "POST",
    headers: { Authorization: "Bearer s3cret", "Content-Type": "application/json" },
    body: JSON.stringify({ events }),
  });
}

// db where INSERT OR IGNORE reports changes===1 only for dids in `newDids`.
function mockDb(newDids: Set<string>) {
  const calls: Call[] = [];
  const stmt = (sql: string): FakeStmt => {
    const s: FakeStmt = {
      sql, binds: [],
      bind(...a) { s.binds = a; return s; },
      async all() { return { results: [] }; },
      async first() { return null; },
      async run() { calls.push({ sql, binds: s.binds }); return { meta: { changes: 1 } }; },
    };
    return s;
  };
  const db: TursoDB = {
    prepare: (sql: string) => stmt(sql),
    async batch(stmts: FakeStmt[]) {
      return stmts.map((st) => {
        calls.push({ sql: st.sql, binds: st.binds });
        const isProbe = /INSERT OR IGNORE INTO actors/.test(st.sql);
        const did = st.binds[0];
        const changes = isProbe && !newDids.has(String(did)) ? 0 : 1;
        return { results: [], meta: { changes } };
      });
    },
  };
  return { db, calls };
}

test("counts ALL new actors — bare-DID AND handle-bearing — not just bare", async () => {
  const events = [
    { did: "did:plc:bareNew" },                                  // new, bare
    { did: "did:plc:handleNew", handle: "new.bsky.social" },     // new, profile-bearing
    { did: "did:plc:handleOld", handle: "old.bsky.social" },     // existing, profile-bearing
  ];
  const { db, calls } = mockDb(new Set(["did:plc:bareNew", "did:plc:handleNew"]));
  const { ctx, promises } = fakeCtx();
  const env = fakeEnv({ ADMIN_SECRET: "s3cret" });

  const res = await handleIngest(mockReq(events), db, env, ctx);
  expect(res.status).toBe(200);
  await Promise.all(promises); // let recordActorDelta's counter write run

  // the counter UPDATE bind is newActors — must be 2 (old code would give 1)
  const counterWrite = calls.find((c) => /UPDATE counters SET value = value \+ \?1/.test(c.sql));
  expect(counterWrite).toBeDefined();
  expect(counterWrite!.binds[0]).toBe(2);
});

test("counts zero new when all actors already exist", async () => {
  const events = [
    { did: "did:plc:a", handle: "a.bsky.social" },
    { did: "did:plc:b" },
  ];
  const { db, calls } = mockDb(new Set()); // nothing new
  const { ctx, promises } = fakeCtx();
  const env = fakeEnv({ ADMIN_SECRET: "s3cret" });

  const res = await handleIngest(mockReq(events), db, env, ctx);
  expect(res.status).toBe(200);
  await Promise.all(promises);

  // newActors === 0 → no counter write at all
  const counterWrite = calls.find((c) => /UPDATE counters SET value = value \+ \?1/.test(c.sql));
  expect(counterWrite).toBeUndefined();
});

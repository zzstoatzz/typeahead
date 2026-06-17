import { test, expect } from "bun:test";
import { handleIngest } from "./ingest";

// Regression for the 2026-06-17 actors_total undercount: new actors first seen
// via a profile/identity event (handle-bearing) went through the upsert path and
// were NEVER counted — only bare-DID inserts were. The fix probes every event
// with INSERT OR IGNORE and counts changes===1, so ALL new actors are counted.

function mockReq(events: any[]) {
  return new Request("https://x/admin/ingest", {
    method: "POST",
    headers: { Authorization: "Bearer s3cret", "Content-Type": "application/json" },
    body: JSON.stringify({ events }),
  });
}

// db where INSERT OR IGNORE reports changes===1 only for dids in `newDids`.
function mockDb(newDids: Set<string>) {
  const calls: { sql: string; binds: any[] }[] = [];
  const stmt = (sql: string) => {
    const s: any = { sql, binds: [] as any[], bind(...a: any[]) { s.binds = a; return s; },
      async run() { calls.push({ sql, binds: s.binds }); return { meta: { changes: 1 } }; } };
    return s;
  };
  return {
    db: {
      prepare: (sql: string) => stmt(sql),
      async batch(stmts: any[]) {
        return stmts.map((st) => {
          calls.push({ sql: st.sql, binds: st.binds ?? [] });
          const isProbe = /INSERT OR IGNORE INTO actors/.test(st.sql);
          const did = st.binds?.[0];
          const changes = isProbe ? (newDids.has(did) ? 1 : 0) : 1;
          return { meta: { changes } };
        });
      },
    } as any,
    calls,
  };
}

test("counts ALL new actors — bare-DID AND handle-bearing — not just bare", async () => {
  const events = [
    { did: "did:plc:bareNew" },                                  // new, bare
    { did: "did:plc:handleNew", handle: "new.bsky.social" },     // new, profile-bearing
    { did: "did:plc:handleOld", handle: "old.bsky.social" },     // existing, profile-bearing
  ];
  const { db, calls } = mockDb(new Set(["did:plc:bareNew", "did:plc:handleNew"]));
  const promises: Promise<any>[] = [];
  const ctx: any = { waitUntil: (p: Promise<any>) => promises.push(p) };
  const env: any = { ADMIN_SECRET: "s3cret" };

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
  const promises: Promise<any>[] = [];
  const ctx: any = { waitUntil: (p: Promise<any>) => promises.push(p) };
  const env: any = { ADMIN_SECRET: "s3cret" };

  const res = await handleIngest(mockReq(events), db, env, ctx);
  expect(res.status).toBe(200);
  await Promise.all(promises);

  // newActors === 0 → no counter write at all
  const counterWrite = calls.find((c) => /UPDATE counters SET value = value \+ \?1/.test(c.sql));
  expect(counterWrite).toBeUndefined();
});

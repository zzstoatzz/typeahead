import { test, expect } from "bun:test";
import { Database, type SQLQueryBindings } from "bun:sqlite";
import type { Row } from "@libsql/client/web";
import { handleIngest } from "./handlers/ingest";
import {
  handleIdentityComplete, handleIdentityLease, handleIdentityStatus, handlePlcOperations, LEASE_SECONDS, MAX_ATTEMPTS, MAX_RETRY_SECONDS,
  type IdentityResult,
} from "./identity-queue";
import type { Stmt, TursoDB } from "./db";
import type { Env, IngestEvent } from "./types";

const SECRET = "s3cret";

/** SQLite with Turso's batch contract: each batch() is one transaction. The
 *  optional fault fails a whole batch, leaving earlier batches committed. */
function sqliteDb() {
  const sqlite = new Database(":memory:");
  sqlite.run(`CREATE TABLE actors (
    did TEXT PRIMARY KEY, handle TEXT NOT NULL DEFAULT '', display_name TEXT DEFAULT '',
    avatar_url TEXT DEFAULT '', pds TEXT DEFAULT '', description TEXT DEFAULT '',
    banner_url TEXT DEFAULT '', updated_at INTEGER DEFAULT 0,
    profile_checked_at INTEGER DEFAULT 0, last_activity_at INTEGER DEFAULT 0,
    identity_resolved_at INTEGER NOT NULL DEFAULT 0)`);
  sqlite.run(`CREATE TABLE actor_deltas (bucket INTEGER PRIMARY KEY, actors_delta INTEGER DEFAULT 0,
    handles_delta INTEGER DEFAULT 0, avatars_delta INTEGER DEFAULT 0)`);
  sqlite.run("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER)");
  sqlite.run(`CREATE TABLE identity_queue (did TEXT PRIMARY KEY, gen INTEGER NOT NULL DEFAULT 1,
    due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
    enqueued_at INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '')`);
  sqlite.run("CREATE INDEX idx_identity_queue_due ON identity_queue(due_at)");

  type Prepared = Stmt & { exec(): { rows: Row[]; changes: number } };
  const prepare = (sql: string): Prepared => {
    let binds: SQLQueryBindings[] = [];
    const s: Prepared = {
      bind(...a) {
        // SAFETY: the statements under test bind only strings and integers,
        // which libsql's InValue and bun's bindings both accept unchanged.
        binds = a as SQLQueryBindings[];
        return s;
      },
      exec() {
        const q = sqlite.prepare(sql);
        if (/^\s*(SELECT|.*RETURNING)/s.test(sql)) {
          // SAFETY: bun returns one plain object per row keyed by column name,
          // the same shape libsql's Row exposes to rowsAs().
          const rows = q.all(...binds) as Row[];
          return { rows, changes: rows.length };
        }
        return { rows: [], changes: q.run(...binds).changes };
      },
      async all<T>() {
        // SAFETY: T is the caller's declaration of its own SELECT's columns.
        return { results: s.exec().rows as T[] };
      },
      async first<T>() {
        // SAFETY: as all().
        return (s.exec().rows[0] as T | undefined) ?? null;
      },
      async run() { return { meta: { changes: s.exec().changes } }; },
    };
    return s;
  };

  let batches = 0;
  let failBatch: number | null = null;
  const db: TursoDB = {
    prepare,
    async batch(stmts) {
      batches++;
      if (failBatch === batches) throw new Error("injected batch failure");
      return sqlite.transaction(() => stmts.map((st) => {
        // SAFETY: every Stmt here came from prepare() above.
        const r = (st as Prepared).exec();
        return { results: r.rows, meta: { changes: r.changes } };
      }))();
    },
  };

  const queue = () => sqlite.query<{ did: string; gen: number; due_at: number; attempts: number; reason: string; last_error: string }, []>(
    "SELECT did, gen, due_at, attempts, reason, last_error FROM identity_queue ORDER BY did").all();
  const actor = (did: string) => sqlite.query<{ handle: string; pds: string; identity_resolved_at: number }, [string]>(
    "SELECT handle, pds, identity_resolved_at FROM actors WHERE did = ?").get(did);
  const now = () => sqlite.query<{ t: number }, []>("SELECT unixepoch() AS t").get()!.t;
  const age = (did: string, seconds: number) =>
    sqlite.run("UPDATE identity_queue SET due_at = due_at - ? WHERE did = ?", [seconds, did]);
  return {
    db, sqlite, queue, actor, now, age,
    failOnBatch(n: number) { failBatch = batches + n; },
  };
}

/** the slice of KVNamespace these handlers consume: get and put of one cursor key */
type CursorKv = { get(key: string): Promise<string | null>; put(key: string, value: string): Promise<void> };
type CursorLog = { puts: string[] };

function fakeEnv(kv: CursorLog = { puts: [] }): Env {
  const KV: CursorKv = {
    get: async () => kv.puts.at(-1) ?? null,
    put: async (_key, value) => { kv.puts.push(value); },
  };
  const fields = { ADMIN_SECRET: SECRET, KV };
  // SAFETY: with a matching secret these handlers read only ADMIN_SECRET and
  // KV.get/put of a cursor; every request here carries the secret, so the
  // strict limiter and every other binding are never touched.
  return fields as Env;
}

function fakeCtx(): ExecutionContext {
  const ctx = { waitUntil: (_promise: Promise<unknown>) => {} };
  // SAFETY: handleIngest touches the context only through waitUntil; the
  // runtime-owned members (exports, props, tracing) are never read.
  return ctx as ExecutionContext;
}

type RequestBody =
  | { events: IngestEvent[]; cursor?: number; identity_queue?: boolean }
  | { limit: number }
  | { dids: string[]; cursor: string }
  | { results: IdentityResult[] }
  | { results: { outcome: string; did: string; gen: number; observed_at: number }[] };

function post(path: string, body: RequestBody): Request {
  return new Request(`https://x${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${SECRET}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const ingest = (db: TursoDB, events: IngestEvent[], extra: { cursor?: number; identity_queue?: boolean } = {}, env = fakeEnv()) =>
  handleIngest(post("/admin/ingest", { events, ...extra }), db, env, fakeCtx());

async function lease(db: TursoDB, limit = 50) {
  const res = await handleIdentityLease(post("/admin/identity/lease", { limit }), db, fakeEnv());
  expect(res.status).toBe(200);
  const body: { jobs: { did: string; gen: number; attempts: number; reason: string }[] } = await res.json();
  return body.jobs;
}

async function complete(db: TursoDB, results: IdentityResult[]) {
  const res = await handleIdentityComplete(post("/admin/identity/complete", { results }), db, fakeEnv());
  expect(res.status).toBe(200);
  const body: { ok: true; resolved: number; retried: number; abandoned: number } = await res.json();
  return body;
}

test("#identity signals coalesce into one row per DID and bump its generation", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }, { did: "did:plc:b", identity: "account" }]);
  expect(t.queue().map((r) => [r.did, r.gen, r.reason])).toEqual([
    ["did:plc:a", 2, "identity"],
    ["did:plc:b", 1, "account"],
  ]);
});

test("discovery enqueues only actors this request created, and only when the ingester asks", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:old" }]);
  expect(t.queue()).toEqual([]);
  await ingest(t.db, [{ did: "did:plc:old" }, { did: "did:plc:new", display_name: "New" }], { identity_queue: true });
  expect(t.queue().map((r) => [r.did, r.reason])).toEqual([["did:plc:new", "discovered"]]);
});

test("discovery never resets a pending row's backoff", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:x", identity: "identity" }]);
  t.sqlite.run("UPDATE identity_queue SET due_at = due_at + 3600, attempts = 4");
  t.sqlite.run("DELETE FROM actors");
  await ingest(t.db, [{ did: "did:plc:x" }], { identity_queue: true });
  const [row] = t.queue();
  expect(row!.attempts).toBe(4);
  expect(row!.due_at).toBeGreaterThan(t.now() + 3000);
});

test("a batch that fails partway keeps discovery work for actors it already created", async () => {
  // Chunks commit separately. Discovery work must commit with the probe that
  // created the actor: the ingester's retry finds the actor no longer new.
  const t = sqliteDb();
  const events = Array.from({ length: 200 }, (_, i) => ({ did: `did:plc:n${String(i).padStart(3, "0")}` }));
  const kv: CursorLog = { puts: [] };
  t.failOnBatch(2);
  const failed = await ingest(t.db, events, { identity_queue: true, cursor: 99 }, fakeEnv(kv));
  expect(failed.status).toBe(500);
  expect(kv.puts).toEqual([]);
  const committed = t.queue().length;
  expect(committed).toBeGreaterThan(0);
  expect(committed).toBeLessThan(events.length);

  const retried = await ingest(t.db, events, { identity_queue: true, cursor: 99 }, fakeEnv(kv));
  expect(retried.status).toBe(200);
  expect(kv.puts).toEqual(["99"]);
  expect(t.queue().length).toBe(events.length);
});

test("a lease hides its work until it lapses, so a crashed resolver's work returns", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }, { did: "did:plc:b", identity: "identity" }]);
  const first = await lease(t.db);
  expect(first.map((j) => [j.did, j.attempts])).toEqual([["did:plc:a", 1], ["did:plc:b", 1]]);
  expect(await lease(t.db)).toEqual([]);

  t.age("did:plc:a", LEASE_SECONDS);
  const again = await lease(t.db);
  expect(again.map((j) => [j.did, j.attempts])).toEqual([["did:plc:a", 2]]);
});

test("a resolved result writes the identity and settles its generation", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [job] = await lease(t.db);
  const observed = t.now();
  const body = await complete(t.db, [{
    outcome: "resolved", did: job!.did, gen: job!.gen, observed_at: observed,
    handle: "alice.example", handle_verified: true, pds: "https://pds.example",
  }]);
  expect(body.resolved).toBe(1);
  expect(t.actor("did:plc:a")).toEqual({ handle: "alice.example", pds: "https://pds.example", identity_resolved_at: observed });
  expect(t.queue()).toEqual([]);
});

test("a signal that arrives during resolution survives the older completion", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [job] = await lease(t.db);
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  await complete(t.db, [{ outcome: "resolved", did: "did:plc:a", gen: job!.gen, observed_at: t.now(), handle: "old.example", handle_verified: true }]);
  const [row] = t.queue();
  expect(row!.gen).toBe(job!.gen + 1);
  expect(row!.due_at).toBeLessThanOrEqual(t.now());
  expect(t.actor("did:plc:a")!.handle).toBe("old.example");
});

test("an older observation never overwrites a newer one", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [first] = await lease(t.db);
  const newer = t.now();
  await complete(t.db, [{ outcome: "resolved", did: "did:plc:a", gen: first!.gen, observed_at: newer, pds: "https://new.example" }]);
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [second] = await lease(t.db);
  await complete(t.db, [{ outcome: "resolved", did: "did:plc:a", gen: second!.gen, observed_at: newer - 60, pds: "https://old.example" }]);
  expect(t.actor("did:plc:a")).toEqual({ handle: "", pds: "https://new.example", identity_resolved_at: newer });
  expect(t.queue()).toEqual([]);
});

test("unverified handles are not written; handle.invalid is", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", handle: "real.example", handle_verified: true, identity: "identity" }]);
  const [job] = await lease(t.db);
  await complete(t.db, [{ outcome: "resolved", did: "did:plc:a", gen: job!.gen, observed_at: t.now(), handle: "claimed.example", handle_verified: false }]);
  expect(t.actor("did:plc:a")!.handle).toBe("real.example");

  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [next] = await lease(t.db);
  await complete(t.db, [{ outcome: "resolved", did: "did:plc:a", gen: next!.gen, observed_at: t.now(), handle: "handle.invalid" }]);
  expect(t.actor("did:plc:a")!.handle).toBe("handle.invalid");
});

test("a retry keeps the row, records the PDS it learned, and clamps the delay", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [job] = await lease(t.db);
  const body = await complete(t.db, [{
    outcome: "retry", did: "did:plc:a", gen: job!.gen, attempts: job!.attempts, observed_at: t.now(),
    delay_seconds: 10 * MAX_RETRY_SECONDS, pds: "https://pds.example", error: "handle_unverified",
  }]);
  expect(body.retried).toBe(1);
  const [row] = t.queue();
  expect(row!.last_error).toBe("handle_unverified");
  expect(row!.due_at).toBeLessThanOrEqual(t.now() + MAX_RETRY_SECONDS);
  expect(row!.due_at).toBeGreaterThan(t.now() + MAX_RETRY_SECONDS - 5);
  expect(t.actor("did:plc:a")!.pds).toBe("https://pds.example");
});

test("work that keeps failing is abandoned after its attempt budget", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const [job] = await lease(t.db);
  const body = await complete(t.db, [{
    outcome: "retry", did: "did:plc:a", gen: job!.gen, attempts: MAX_ATTEMPTS, observed_at: t.now(),
    delay_seconds: 60, error: "did_unavailable",
  }]);
  expect(body.abandoned).toBe(1);
  expect(t.queue()).toEqual([]);
});

test("malformed results are rejected without writing anything", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const res = await handleIdentityComplete(post("/admin/identity/complete", { results: [{ outcome: "resolved", did: "not-a-did", gen: 1, observed_at: 1 }] }), t.db, fakeEnv());
  expect(res.status).toBe(400);
  expect(t.queue().length).toBe(1);
});

const KNOWN = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const UNKNOWN = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";

test("PLC operations re-request resolution only for accounts already indexed", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: KNOWN }]);
  const kv: CursorLog = { puts: [] };
  const res = await handlePlcOperations(post("/admin/identity/plc", { dids: [KNOWN, UNKNOWN, KNOWN], cursor: "2026-10-31T00:00:00.000Z" }), t.db, fakeEnv(kv));
  expect(res.status).toBe(200);
  expect(t.queue().map((r) => [r.did, r.reason])).toEqual([[KNOWN, "plc"]]);
  expect(kv.puts).toEqual(["2026-10-31T00:00:00.000Z"]);
});

test("the PLC cursor only moves forward", async () => {
  const t = sqliteDb();
  const kv: CursorLog = { puts: ["2026-10-31T12:00:00.000Z"] };
  await handlePlcOperations(post("/admin/identity/plc", { dids: [], cursor: "2026-10-31T11:00:00.000Z" }), t.db, fakeEnv(kv));
  expect(kv.puts).toEqual(["2026-10-31T12:00:00.000Z"]);
  await handlePlcOperations(post("/admin/identity/plc", { dids: [], cursor: "2026-10-31T13:00:00.000Z" }), t.db, fakeEnv(kv));
  expect(kv.puts.at(-1)).toBe("2026-10-31T13:00:00.000Z");
});

test("a page whose enqueue fails does not advance the PLC cursor", async () => {
  const t = sqliteDb();
  await ingest(t.db, [{ did: KNOWN }]);
  const kv: CursorLog = { puts: [] };
  t.failOnBatch(1);
  const res = await handlePlcOperations(post("/admin/identity/plc", { dids: [KNOWN], cursor: "2026-10-31T00:00:00.000Z" }), t.db, fakeEnv(kv));
  expect(res.status).toBe(500);
  expect(kv.puts).toEqual([]);
  expect(t.queue()).toEqual([]);
});

test("queue status reads zero, not null, when nothing is pending", async () => {
  const t = sqliteDb();
  const get = () => new Request("https://x/admin/identity/status", { headers: { Authorization: `Bearer ${SECRET}` } });
  const empty: { pending: number; due: number; oldest_due_at: number | null } = await (await handleIdentityStatus(get(), t.db, fakeEnv())).json();
  expect(empty).toEqual({ pending: 0, due: 0, oldest_due_at: null });
  await ingest(t.db, [{ did: "did:plc:a", identity: "identity" }]);
  const one: { pending: number; due: number } = await (await handleIdentityStatus(get(), t.db, fakeEnv())).json();
  expect([one.pending, one.due]).toEqual([1, 1]);
});

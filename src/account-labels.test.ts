import { test, expect } from "bun:test";
import { Database, type SQLQueryBindings } from "bun:sqlite";
import type { Row } from "@libsql/client/web";
import { handleLabelCursor, handleLabelReconcile, handleLabelStream, labelSubject } from "./account-labels";
import type { Stmt, SqlDb } from "./db";
import { BSKY_MOD_DID } from "./moderation";
import type { Env } from "./types";

const SECRET = "s3cret";
const ALICE = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const BOB = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
const STRANGER = "did:plc:cccccccccccccccccccccccc";
const SELF_LABEL = { src: ALICE, uri: `at://${ALICE}/app.bsky.actor.profile/self`, val: "!no-unauthenticated", cts: "2026-01-01T00:00:00.000Z" };

/** SQLite with Turso's batch contract: each batch() is one transaction. The
 *  optional fault fails a whole batch, leaving earlier batches committed. */
function sqliteDb() {
  const sqlite = new Database(":memory:");
  sqlite.run(`CREATE TABLE actors (did TEXT PRIMARY KEY, handle TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]', hidden INTEGER NOT NULL DEFAULT 0, updated_at INTEGER DEFAULT 0)`);
  sqlite.run("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)");
  sqlite.run("CREATE TABLE mod_overrides (did TEXT PRIMARY KEY, action TEXT NOT NULL)");
  sqlite.run("CREATE TABLE blocked_domains (domain TEXT PRIMARY KEY)");
  sqlite.run(`CREATE TABLE account_labels (did TEXT NOT NULL, uri TEXT NOT NULL, val TEXT NOT NULL, cid TEXT,
    neg INTEGER NOT NULL DEFAULT 0, cts TEXT NOT NULL, exp TEXT, seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (did, uri, val))`);
  sqlite.run("INSERT INTO actors (did, handle, labels) VALUES (?, 'alice.test', ?), (?, 'bob.test', '[]')",
    [ALICE, JSON.stringify([SELF_LABEL]), BOB]);

  type Prepared = Stmt & { exec(): { rows: Row[]; changes: number } };
  const prepare = (sql: string): Prepared => {
    let binds: SQLQueryBindings[] = [];
    const s: Prepared = {
      bind(...a) {
        // SAFETY: the statements under test bind only strings, integers and
        // null, which libsql's InValue and bun's bindings both accept unchanged.
        binds = a as SQLQueryBindings[];
        return s;
      },
      exec() {
        const q = sqlite.prepare(sql);
        if (/^\s*SELECT/.test(sql)) {
          // SAFETY: bun returns one plain object per row keyed by column name,
          // the same shape libsql's Row exposes to rowsAs().
          const rows = q.all(...binds) as Row[];
          return { rows, changes: 0 };
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
  const db: SqlDb = {
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

  type Actor = { labels: string; hidden: number; updated_at: number };
  const actor = (did: string) => sqlite.query<Actor, [string]>("SELECT labels, hidden, updated_at FROM actors WHERE did = ?").get(did)!;
  const labelsOf = (did: string) => {
    const parsed: { src: string; uri: string; val: string; exp?: string }[] = JSON.parse(actor(did).labels);
    return parsed.map((l) => `${l.src === BSKY_MOD_DID ? "mod" : "self"}:${l.uri === did ? "account" : "profile"}:${l.val}`).sort();
  };
  const stored = () => sqlite.query<{ did: string; val: string; neg: number; seq: number }, []>(
    "SELECT did, val, neg, seq FROM account_labels ORDER BY did, uri, val").all();
  return {
    db, sqlite, actor, labelsOf, stored,
    failOnBatch(n: number) { failBatch = batches + n; },
  };
}

function fakeEnv(): Env {
  const fields = { ADMIN_SECRET: SECRET };
  // SAFETY: with a matching secret these handlers read only ADMIN_SECRET;
  // every request here carries it, so the limiter is never touched.
  return fields as Env;
}

type Frame = { uri: string; val: string; seq: number; neg?: boolean; exp?: string; cts?: string; src?: string };
type WireLabel = { src: string; uri: string; val: string; cts: string; neg?: boolean; exp?: string };
function wire({ uri, val, seq, neg, exp, cts, src }: Frame): WireLabel {
  const label: WireLabel = { src: src ?? BSKY_MOD_DID, uri, val, cts: cts ?? `2026-11-01T00:00:${String(seq % 60).padStart(2, "0")}.000Z` };
  if (neg) label.neg = neg;
  if (exp) label.exp = exp;
  return label;
}
const frame = (f: Frame) => ({ ...wire(f), seq: f.seq });
const answer = (f: Frame) => wire(f);

type StreamBody = { labels: object[]; cursor: number };
type ReconcileBody = { dids: string[]; labels: object[] };

function request(path: string, body: StreamBody | ReconcileBody): Request {
  return new Request(`https://x${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${SECRET}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
const stream = (db: SqlDb, labels: object[], cursor: number) => handleLabelStream(request("/admin/labels", { labels, cursor }), db, fakeEnv());
const reconcile = (db: SqlDb, dids: string[], labels: object[]) => handleLabelReconcile(request("/admin/labels/reconcile", { dids, labels }), db, fakeEnv());
async function cursor(db: SqlDb): Promise<number | null> {
  const res = await handleLabelCursor(new Request("https://x/admin/labels/cursor", { headers: { Authorization: `Bearer ${SECRET}` } }), db, fakeEnv());
  const body: { cursor: number | null } = await res.json();
  return body.cursor;
}

test("acts only on Bluesky moderation labels about an account or its profile record", () => {
  expect(labelSubject({ src: BSKY_MOD_DID, uri: ALICE })).toBe(ALICE);
  expect(labelSubject({ src: BSKY_MOD_DID, uri: `at://${ALICE}/app.bsky.actor.profile/self` })).toBe(ALICE);
  expect(labelSubject({ src: BSKY_MOD_DID, uri: `at://${ALICE}/app.bsky.feed.post/3kabc` })).toBeNull();
  expect(labelSubject({ src: "did:plc:someotherlabelerxxxxxxxx", uri: ALICE })).toBeNull();
});

test("a stream label hides the account and keeps the account's self-labels", async () => {
  const t = sqliteDb();
  const res = await stream(t.db, [frame({ uri: ALICE, val: "spam", seq: 10 })], 10);
  expect(res.status).toBe(200);
  expect(t.labelsOf(ALICE)).toEqual(["mod:account:spam", "self:profile:!no-unauthenticated"]);
  expect(t.actor(ALICE).hidden).toBe(1);
  expect(t.actor(ALICE).updated_at).toBeGreaterThan(0);
  expect(await cursor(t.db)).toBe(10);
});

test("a negation withdraws the label and a delayed older assertion cannot restore it", async () => {
  const t = sqliteDb();
  await stream(t.db, [frame({ uri: ALICE, val: "!hide", seq: 10 })], 10);
  await stream(t.db, [frame({ uri: ALICE, val: "!hide", seq: 12, neg: true })], 12);
  expect(t.labelsOf(ALICE)).toEqual(["self:profile:!no-unauthenticated"]);
  expect(t.actor(ALICE).hidden).toBe(0);

  await stream(t.db, [frame({ uri: ALICE, val: "!hide", seq: 11 })], 11);
  expect(t.labelsOf(ALICE)).toEqual(["self:profile:!no-unauthenticated"]);
  expect(t.stored()).toEqual([{ did: ALICE, val: "!hide", neg: 1, seq: 12 }]);
  expect(await cursor(t.db)).toBe(12);
});

test("within one batch the higher seq wins whatever the order", async () => {
  const t = sqliteDb();
  await stream(t.db, [
    frame({ uri: BOB, val: "spam", seq: 21, neg: true }),
    frame({ uri: BOB, val: "spam", seq: 20 }),
  ], 21);
  expect(t.labelsOf(BOB)).toEqual([]);
  expect(t.actor(BOB).hidden).toBe(0);
});

test("labels for accounts we do not index, other labelers and malformed frames are skipped", async () => {
  const t = sqliteDb();
  const res = await stream(t.db, [
    frame({ uri: STRANGER, val: "spam", seq: 1 }),
    frame({ uri: ALICE, val: "spam", seq: 2, src: "did:plc:someotherlabelerxxxxxxxx" }),
    { uri: ALICE, val: "spam", seq: 3 },
    frame({ uri: BOB, val: "porn", seq: 4 }),
  ], 4);
  const body: { accepted: number; ignored: number } = await res.json();
  expect(body).toMatchObject({ accepted: 2, ignored: 2 });
  expect(t.stored()).toEqual([{ did: BOB, val: "porn", neg: 0, seq: 4 }]);
  expect(t.labelsOf(BOB)).toEqual(["mod:account:porn"]);
  expect(t.actor(BOB).hidden).toBe(0);
  expect(await cursor(t.db)).toBe(4);
});

test("a profile-record label is stored with its record uri", async () => {
  const t = sqliteDb();
  await stream(t.db, [frame({ uri: `at://${BOB}/app.bsky.actor.profile/self`, val: "!takedown", seq: 5 })], 5);
  expect(t.labelsOf(BOB)).toEqual(["mod:profile:!takedown"]);
});

test("an expiring label is projected with its expiry and stops hiding once it lapses", async () => {
  const t = sqliteDb();
  await stream(t.db, [frame({ uri: BOB, val: "!hide", seq: 6, exp: "2020-01-01T00:00:00.000Z" })], 6);
  const [projected] = JSON.parse(t.actor(BOB).labels);
  expect(projected).toEqual({ src: BSKY_MOD_DID, uri: BOB, val: "!hide", cts: expect.any(String), exp: "2020-01-01T00:00:00.000Z" });
  expect(t.actor(BOB).hidden).toBe(0);
});

test("a crash before the cursor commits replays the batch without double effects", async () => {
  const t = sqliteDb();
  const batch = [frame({ uri: ALICE, val: "spam", seq: 30 }), frame({ uri: BOB, val: "spam", seq: 31 })];
  t.failOnBatch(1);
  expect((await stream(t.db, batch, 31)).status).toBe(500);
  expect(await cursor(t.db)).toBeNull();

  await stream(t.db, batch, 31);
  const once = { alice: t.actor(ALICE).labels, bob: t.actor(BOB).labels, stored: t.stored() };
  await stream(t.db, batch, 31);
  expect({ alice: t.actor(ALICE).labels, bob: t.actor(BOB).labels, stored: t.stored() }).toEqual(once);
  expect(t.labelsOf(ALICE)).toEqual(["mod:account:spam", "self:profile:!no-unauthenticated"]);
  expect(await cursor(t.db)).toBe(31);
});

test("the cursor never moves backward", async () => {
  const t = sqliteDb();
  await stream(t.db, [], 50);
  await stream(t.db, [], 40);
  expect(await cursor(t.db)).toBe(50);
});

test("reconciliation never overwrites a stream row", async () => {
  const t = sqliteDb();
  await stream(t.db, [frame({ uri: ALICE, val: "spam", seq: 40, neg: true })], 40);
  await reconcile(t.db, [ALICE], [answer({ uri: ALICE, val: "spam", seq: 0 })]);
  expect(t.stored()).toEqual([{ did: ALICE, val: "spam", neg: 1, seq: 40 }]);
  expect(t.actor(ALICE).hidden).toBe(0);
});

test("reconciliation folds label history and replaces its own earlier answer", async () => {
  const t = sqliteDb();
  await reconcile(t.db, [BOB], [
    answer({ uri: BOB, val: "spam", seq: 0, cts: "2026-03-01T00:00:00.000Z" }),
    answer({ uri: BOB, val: "spam", seq: 0, cts: "2026-04-01T00:00:00.000Z", neg: true }),
    answer({ uri: BOB, val: "!hide", seq: 0, cts: "2026-05-01T00:00:00.000Z" }),
  ]);
  expect(t.labelsOf(BOB)).toEqual(["mod:account:!hide"]);
  expect(t.actor(BOB).hidden).toBe(1);

  await reconcile(t.db, [BOB], []);
  expect(t.stored()).toEqual([]);
  expect(t.labelsOf(BOB)).toEqual([]);
  expect(t.actor(BOB).hidden).toBe(0);
});

test("reconciliation clears moderation labels a profile fetch left behind", async () => {
  const t = sqliteDb();
  const stale = [SELF_LABEL, { src: BSKY_MOD_DID, uri: ALICE, val: "spam", cts: "2026-01-01T00:00:00.000Z" }];
  t.sqlite.run("UPDATE actors SET labels = ?, hidden = 1 WHERE did = ?", [JSON.stringify(stale), ALICE]);
  await reconcile(t.db, [ALICE], []);
  expect(t.labelsOf(ALICE)).toEqual(["self:profile:!no-unauthenticated"]);
  expect(t.actor(ALICE).hidden).toBe(0);
});

test("an unchanged account is not rewritten", async () => {
  const t = sqliteDb();
  await reconcile(t.db, [ALICE, BOB], []);
  expect(t.actor(ALICE).updated_at).toBe(0);
  expect(t.actor(BOB).updated_at).toBe(0);
});

test("a show override keeps a labelled account visible", async () => {
  const t = sqliteDb();
  t.sqlite.run("INSERT INTO mod_overrides (did, action) VALUES (?, 'show')", [BOB]);
  await stream(t.db, [frame({ uri: BOB, val: "spam", seq: 60 })], 60);
  expect(t.labelsOf(BOB)).toEqual(["mod:account:spam"]);
  expect(t.actor(BOB).hidden).toBe(0);
});

test("reconciliation ignores labels about accounts it did not ask about", async () => {
  const t = sqliteDb();
  const res = await reconcile(t.db, [BOB], [answer({ uri: ALICE, val: "spam", seq: 0 })]);
  const body: { accepted: number; ignored: number } = await res.json();
  expect(body).toMatchObject({ accepted: 0, ignored: 1 });
  expect(t.labelsOf(ALICE)).toEqual(["self:profile:!no-unauthenticated"]);
});

test("a batch split across transactions replays cleanly when the cursor's transaction fails", async () => {
  const t = sqliteDb();
  const dids = Array.from({ length: 150 }, (_, i) => `did:plc:${"d".repeat(20)}${String(i).padStart(4, "0").replace(/\d/g, (c) => "abcdefghij"[Number(c)]!)}`);
  for (const d of dids) t.sqlite.run("INSERT INTO actors (did, handle) VALUES (?, 'x.test')", [d]);
  const batch = dids.map((uri, i) => frame({ uri, val: "spam", seq: 100 + i }));

  t.failOnBatch(2);
  expect((await stream(t.db, batch, 249)).status).toBe(500);
  expect(t.stored().length).toBeGreaterThan(0);
  expect(t.stored().length).toBeLessThan(dids.length);
  expect(await cursor(t.db)).toBeNull();

  await stream(t.db, batch, 249);
  expect(t.stored().length).toBe(dids.length);
  expect(dids.every((d) => t.actor(d).hidden === 1)).toBe(true);
  expect(await cursor(t.db)).toBe(249);
});

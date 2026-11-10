import { expect, test } from "bun:test";
import { Database, type SQLQueryBindings } from "bun:sqlite";
import type { Row } from "@libsql/client/web";
import type { Stmt, SqlDb } from "./db";
import { BSKY_MOD_DID } from "./moderation";
import { EXCLUSIONS_KEY, EXCLUSIONS_REFRESH_MS, parseExclusions, recordExclusions } from "./exclusions";

function sqliteDb() {
  const sqlite = new Database(":memory:");
  sqlite.run("CREATE TABLE actors (did TEXT PRIMARY KEY, handle TEXT NOT NULL DEFAULT '', labels TEXT DEFAULT '[]', hidden INTEGER DEFAULT 0)");
  sqlite.run("CREATE TABLE mod_overrides (did TEXT PRIMARY KEY, action TEXT NOT NULL)");
  sqlite.run("CREATE TABLE blocked_domains (domain TEXT PRIMARY KEY)");
  const prepare = (sql: string): Stmt & { rows(): Row[] } => {
    let binds: SQLQueryBindings[] = [];
    const s: Stmt & { rows(): Row[] } = {
      bind(...a) {
        // SAFETY: these statements bind only DID strings.
        binds = a as SQLQueryBindings[];
        return s;
      },
      rows() {
        // SAFETY: bun returns one plain object per row keyed by column name, as libsql's Row does.
        return sqlite.prepare(sql).all(...binds) as Row[];
      },
      async all<T>() {
        // SAFETY: T is the caller's declaration of its own SELECT's columns.
        return { results: s.rows() as T[] };
      },
      async first<T>() {
        // SAFETY: as all().
        return (s.rows()[0] as T | undefined) ?? null;
      },
      async run() { return { meta: { changes: 0 } }; },
    };
    return s;
  };
  const db: SqlDb = {
    prepare,
    async batch(stmts) {
      return stmts.map((st) => {
        // SAFETY: every Stmt here came from prepare() above.
        const prepared = st as ReturnType<typeof prepare>;
        return { results: prepared.rows(), meta: { changes: 0 } };
      });
    },
  };
  return { db, sqlite };
}

function memoryKv() {
  const store = new Map<string, string>();
  return {
    store,
    get: async (key: string) => store.get(key) ?? null,
    put: async (key: string, value: string) => { store.set(key, value); },
  };
}

const label = (val: string, extra: { exp?: string } = {}) =>
  JSON.stringify([{ src: BSKY_MOD_DID, val, uri: "did:plc:x", cts: "2026-01-01T00:00:00Z", ...extra }]);

function seed(sqlite: Database) {
  const actor = sqlite.prepare("INSERT INTO actors (did, handle, labels, hidden) VALUES (?, ?, ?, ?)");
  actor.run("did:plc:spam", "spam.bsky.social", label("spam"), 1);
  actor.run("did:plc:stale", "stale.bsky.social", "[]", 1);
  actor.run("did:plc:expired", "expired.bsky.social", label("!hide", { exp: "2026-01-02T00:00:00Z" }), 1);
  actor.run("did:plc:blocked", "x.blocked.example", "[]", 1);
  actor.run("did:plc:shown", "shown.bsky.social", label("spam"), 1);
  actor.run("did:plc:forced", "forced.bsky.social", "[]", 0);
  actor.run("did:plc:visible", "visible.bsky.social", "[]", 0);
  actor.run("did:plc:invalid-hidden", "handle.invalid", label("spam"), 1);
  actor.run("did:plc:invalid", "handle.invalid", "[]", 0);
  actor.run("did:plc:wave1", "donald.trump.com", "[]", 0);
  actor.run("did:plc:wave2", "Donald.Trump.com", "[]", 0);
  actor.run("did:plc:unresolved", "", label("spam"), 1);
  sqlite.run("INSERT INTO blocked_domains (domain) VALUES ('blocked.example')");
  sqlite.run("INSERT INTO mod_overrides (did, action) VALUES ('did:plc:shown', 'show'), ('did:plc:forced', 'hide')");
}

test("counts by the serving rule, not the stored hidden flag", async () => {
  const { db, sqlite } = sqliteDb();
  seed(sqlite);
  const kv = memoryKv();
  const counted = await recordExclusions(db, kv, new Date("2026-11-01T09:00:00Z"));
  expect(counted).toEqual({
    version: 2,
    as_of: "2026-11-01T09:00:00.000Z",
    // spam label, blocked subdomain, hide override on an unflagged row
    policy_hidden: 3,
    // no label, an expired label, a show override
    stale_hidden_flags: 3,
    handle_invalid: 2,
    quarantined: 2,
  });
  expect(parseExclusions(kv.store.get(EXCLUSIONS_KEY) ?? null)).toEqual(counted);
});

test("recounts only once a day", async () => {
  const { db, sqlite } = sqliteDb();
  seed(sqlite);
  const kv = memoryKv();
  const first = new Date("2026-11-01T09:00:00Z");
  await recordExclusions(db, kv, first);
  sqlite.run("INSERT INTO actors (did, handle, labels, hidden) VALUES ('did:plc:new', 'new.bsky.social', ?, 1)", [label("spam")]);

  expect(await recordExclusions(db, kv, new Date(first.getTime() + EXCLUSIONS_REFRESH_MS - 1))).toBeNull();
  expect(parseExclusions(kv.store.get(EXCLUSIONS_KEY) ?? null)?.policy_hidden).toBe(3);

  const later = await recordExclusions(db, kv, new Date(first.getTime() + EXCLUSIONS_REFRESH_MS));
  expect(later?.policy_hidden).toBe(4);
});

test("an unreadable or older stored shape is treated as missing", () => {
  expect(parseExclusions(null)).toBeNull();
  expect(parseExclusions("not json")).toBeNull();
  expect(parseExclusions(JSON.stringify({ count: 3110, as_of: "2026-10-28" }))).toBeNull();
});

import { describe, test, expect } from "bun:test";
import { Miniflare } from "miniflare";
import { d1Db, rowsAs } from "./db";

async function localD1() {
  const mf = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response(null) } }",
    d1Databases: ["DB"],
  });
  const binding = await mf.getD1Database("DB");
  await binding.prepare(
    "CREATE TABLE hits (bucket INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0, ms REAL NOT NULL DEFAULT 0, PRIMARY KEY (bucket, domain))",
  ).run();
  return { db: d1Db(binding), dispose: () => mf.dispose() };
}

const UPSERT = `INSERT INTO hits (bucket, domain, hits, ms) VALUES (?1, ?2, 1, ?3)
  ON CONFLICT(bucket, domain) DO UPDATE SET hits = hits + 1, ms = ms + ?3`;

describe("d1Db against a local D1", () => {
  test("an upsert with reused numbered parameters increments and reports changes", async () => {
    const { db, dispose } = await localD1();
    try {
      const first = await db.prepare(UPSERT).bind(7, "plyr.fm", 1.5).run();
      const second = await db.prepare(UPSERT).bind(7, "plyr.fm", 2.25).run();
      expect([first.meta.changes, second.meta.changes]).toEqual([1, 1]);
      const { results } = await db.prepare("SELECT bucket, domain, hits, ms FROM hits").all<{ bucket: number; domain: string; hits: number; ms: number }>();
      expect(results).toEqual([{ bucket: 7, domain: "plyr.fm", hits: 2, ms: 3.75 }]);
      const none = await db.prepare("DELETE FROM hits WHERE bucket = ?1").bind(8).run();
      expect(none.meta.changes).toBe(0);
    } finally { await dispose(); }
  });

  test("bind replaces the arguments of a prepared statement", async () => {
    const { db, dispose } = await localD1();
    try {
      const stmt = db.prepare(UPSERT);
      await stmt.bind(1, "a.example", 1).run();
      await stmt.bind(2, "b.example", 1).run();
      const { results } = await db.prepare("SELECT domain FROM hits ORDER BY bucket").all<{ domain: string }>();
      expect(results.map((r) => r.domain)).toEqual(["a.example", "b.example"]);
    } finally { await dispose(); }
  });

  test("first returns the row, or null when nothing matches", async () => {
    const { db, dispose } = await localD1();
    try {
      await db.prepare(UPSERT).bind(1, "a.example", 1).run();
      const lookup = db.prepare("SELECT 1 AS ok FROM hits WHERE domain = ?1");
      expect(await lookup.bind("a.example").first<{ ok: number }>()).toEqual({ ok: 1 });
      expect(await lookup.bind("b.example").first<{ ok: number }>()).toBeNull();
    } finally { await dispose(); }
  });

  test("batch returns each statement's rows and changes, in read mode too", async () => {
    const { db, dispose } = await localD1();
    try {
      const written = await db.batch([
        db.prepare(UPSERT).bind(1, "a.example", 1),
        db.prepare(UPSERT).bind(1, "b.example", 1),
        db.prepare("UPDATE hits SET hits = hits + 10 WHERE bucket = ?1").bind(1),
      ]);
      expect(written.map((r) => r.meta.changes)).toEqual([1, 1, 2]);
      const [counted, listed] = await db.batch([
        db.prepare("SELECT count(*) AS n, sum(hits) AS hits FROM hits"),
        db.prepare("SELECT domain FROM hits WHERE bucket >= ?1 ORDER BY domain").bind(1),
      ], "read");
      expect(rowsAs<{ n: number; hits: number }>(counted.results)).toEqual([{ n: 2, hits: 22 }]);
      expect(rowsAs<{ domain: string }>(listed.results)).toEqual([{ domain: "a.example" }, { domain: "b.example" }]);
    } finally { await dispose(); }
  });

  test("a failing statement rolls the whole batch back", async () => {
    const { db, dispose } = await localD1();
    try {
      await expect(db.batch([
        db.prepare(UPSERT).bind(1, "a.example", 1),
        db.prepare("INSERT INTO missing (bucket) VALUES (?1)").bind(1),
      ])).rejects.toThrow("no such table: missing");
      expect(await db.prepare("SELECT count(*) AS n FROM hits").first<{ n: number }>()).toEqual({ n: 0 });
    } finally { await dispose(); }
  });

  test("batch refuses a statement prepared by another database", async () => {
    const { db, dispose } = await localD1();
    const other = await localD1();
    try {
      await expect(db.batch([other.db.prepare("SELECT 1")])).rejects.toThrow("prepared by another database");
    } finally {
      await other.dispose();
      await dispose();
    }
  });
});

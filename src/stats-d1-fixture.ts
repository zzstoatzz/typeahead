import { createClient } from "@libsql/client";
import { Miniflare } from "miniflare";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { d1Db, tursoDb, type SqlDb } from "./db";

const MIGRATIONS = new URL("../migrations/stats-d1/", import.meta.url).pathname;

const PRIMARY_KEYS = {
  actor_deltas: "bucket",
  counters: "name",
  metrics: "hour",
  request_buckets: "bucket, domain, endpoint, status",
  request_daily: "day, domain, endpoint, status",
  snapshots: "hour",
  traffic_buckets: "bucket, domain",
  traffic_daily: "day, domain",
  traffic_sources: "domain",
} as const;

export const STATS_TABLES = Object.keys(PRIMARY_KEYS);

/** Test fixture: two empty stores holding the statistics schema from the D1
 *  migrations, a local D1 and an in-memory libSQL standing in for Turso. */
export async function statsStores() {
  const files = readdirSync(MIGRATIONS).filter((name) => name.endsWith(".sql")).sort();
  const sql = (await Promise.all(files.map((name) => Bun.file(join(MIGRATIONS, name)).text()))).join("\n");
  const mf = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response(null) } }",
    d1Databases: ["STATS_DB"],
  });
  const d1 = await mf.getD1Database("STATS_DB");
  const statements = sql.replace(/--[^\n]*/g, "").split(";").map((statement) => statement.trim()).filter(Boolean);
  await d1.batch(statements.map((statement) => d1.prepare(statement)));
  const client = createClient({ url: "file::memory:" });
  await client.executeMultiple(sql);
  return {
    d1,
    mirror: d1Db(d1),
    turso: tursoDb(client),
    client,
    async dispose() {
      client.close();
      await mf.dispose();
    },
  };
}

/** every row of every statistics table, for comparing one store with another */
export async function statsRows(db: SqlDb) {
  const tables = Object.entries(PRIMARY_KEYS);
  const results = await db.batch(tables.map(([table, key]) => db.prepare(`SELECT * FROM ${table} ORDER BY ${key}`)), "read");
  return tables.map(([table], i) => ({ table, rows: results[i]!.results.map((row) => ({ ...row })) }));
}

export function collectingCtx() {
  const tasks: Promise<unknown>[] = [];
  return { ctx: { waitUntil(task: Promise<unknown>) { tasks.push(task); } }, tasks };
}

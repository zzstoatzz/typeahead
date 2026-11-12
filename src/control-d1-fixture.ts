import { createClient } from "@libsql/client";
import { Miniflare } from "miniflare";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { d1Db, tursoDb } from "./db";

const MIGRATIONS = new URL("../migrations/control-d1/", import.meta.url).pathname;
const TURSO_SCHEMA = new URL("../docs/ops/turso-schema-20261110.sql", import.meta.url).pathname;

/** Test fixture: a local D1 holding the control schema from its migrations. */
export async function controlStore() {
  const files = readdirSync(MIGRATIONS).filter((name) => name.endsWith(".sql")).sort();
  const sql = (await Promise.all(files.map((name) => Bun.file(join(MIGRATIONS, name)).text()))).join("\n");
  const mf = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response(null) } }",
    d1Databases: ["CONTROL_DB"],
  });
  const d1 = await mf.getD1Database("CONTROL_DB");
  const statements = sql.replace(/--[^\n]*/g, "").split(";").map((statement) => statement.trim()).filter(Boolean);
  await d1.batch(statements.map((statement) => d1.prepare(statement)));
  return { d1, control: d1Db(d1), dispose: () => mf.dispose() };
}

/** An in-memory libSQL standing in for Turso, holding production's `actors`,
 *  `mod_overrides` and `blocked_domains` tables as the schema dump defines
 *  them. */
export async function tursoPolicyTables() {
  const dump = await Bun.file(TURSO_SCHEMA).text();
  const tables = ["actors", "mod_overrides", "blocked_domains"].map((name) => {
    const statement = dump.split(";\n").find((candidate) => candidate.startsWith(`CREATE TABLE ${name} (`));
    if (statement === undefined) throw new Error(`the Turso schema dump has no ${name} table`);
    return statement;
  });
  const client = createClient({ url: "file::memory:" });
  await client.executeMultiple(tables.join(";\n") + ";");
  return { turso: tursoDb(client), dispose: () => client.close() };
}

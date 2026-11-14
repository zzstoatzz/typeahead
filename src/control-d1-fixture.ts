import { Miniflare } from "miniflare";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { d1Db } from "./db";

const MIGRATIONS = new URL("../migrations/control-d1/", import.meta.url).pathname;

/** The generation the fixture gives its database, in place of the random one
 *  the migration seeded, so tests can state whole answers. */
export const GENERATION = 4242;

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
  const seeded = await d1.prepare("SELECT value FROM control_meta WHERE name = 'generation'").first<{ value: number }>();
  await d1.prepare("UPDATE control_meta SET value = ?1 WHERE name = 'generation'").bind(GENERATION).run();
  return { d1, control: d1Db(d1), seededGeneration: seeded?.value ?? null, dispose: () => mf.dispose() };
}

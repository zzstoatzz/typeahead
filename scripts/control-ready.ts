import { fileURLToPath, URL } from "node:url";
import { CONTROL_DB_ID } from "../cloudflare.config";
import { META_QUERY, schemaProblem } from "../src/control/control-ready";

async function controlMetadata(options: string[]): Promise<string> {
  const cf = fileURLToPath(new URL("../node_modules/.bin/cf", import.meta.url));
  const child = Bun.spawn([cf, "d1", "raw", CONTROL_DB_ID, "--sql", META_QUERY, ...options], {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    stdout: "pipe", stderr: "pipe",
  });
  const [stdout, stderr, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  if (status !== 0) throw new Error(`cf d1 raw exited ${status}: ${stderr.trim() || stdout.trim()}`);
  return stdout;
}

const target = process.argv.slice(2);
const remote = target[0] === "--remote" && target.length === 1;
const local = target[0] === "--local" && (target.length === 1 || (target.length === 3 && target[1] === "--persist-to"));
if (!remote && !local) {
  console.error("usage: bun scripts/control-ready.ts --remote | --local [--persist-to <dir>]");
  process.exit(2);
}
const placeholderId: string = "00000000-0000-0000-0000-000000000000";
if (CONTROL_DB_ID === placeholderId) {
  console.error("cloudflare.config.ts: put the typeahead-control database ID in CONTROL_DB_ID");
  process.exit(1);
}
const problem = schemaProblem(await controlMetadata(remote ? [] : target).catch((error: Error) => error.message));
if (problem !== null) {
  console.error(`not ready: ${problem}`);
  process.exit(1);
}
console.error("the control database has its schema");

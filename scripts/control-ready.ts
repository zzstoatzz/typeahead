// Whether the typeahead-control D1 database has its schema. Reads only.
//
//   bun scripts/control-ready.ts --remote
//   bun scripts/control-ready.ts --local [--persist-to <dir>]
//
// `just deploy-worker` runs it with --remote first: a Worker deployed against
// a control database without its schema fails every admin policy change and
// logs no indexing request. D1 is read through wrangler.

import { META_QUERY, schemaProblem } from "../src/control-ready";

const DATABASE = "typeahead-control";

async function wrangler(target: string[], command: string): Promise<string> {
  const child = Bun.spawn(["bunx", "wrangler", "d1", "execute", DATABASE, ...target, "--json", "--command", command], {
    stdout: "pipe", stderr: "pipe",
  });
  const [stdout, stderr, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  if (status !== 0) throw new Error(`wrangler d1 execute exited ${status}: ${stderr.trim() || stdout.trim()}`);
  return stdout;
}

const target = process.argv.slice(2);
if (target[0] !== "--remote" && target[0] !== "--local") {
  console.error("usage: bun scripts/control-ready.ts --remote | --local [--persist-to <dir>]");
  process.exit(2);
}
const problem = schemaProblem(await wrangler(target, META_QUERY).catch((error: Error) => error.message));
if (problem !== null) {
  console.error(`not ready: ${problem}`);
  process.exit(1);
}
console.error("the control database has its schema");

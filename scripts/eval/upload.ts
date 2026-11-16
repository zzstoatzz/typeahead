import { mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { publishReports } from "../../src/eval/archive";
import { ROOT, readRuns } from "./archive";
import { wranglerStore } from "./kv";

export async function upload(remote: boolean) {
  await mkdir(ROOT, { recursive: true });
  const lock = join(ROOT, "publisher.lock");
  await mkdir(lock);
  try {
    await writeFile(join(lock, "owner.json"), JSON.stringify({ pid: process.pid, at: new Date().toISOString() }));
    const reports = await readRuns();
    if (!reports.length) throw new Error("refusing to publish empty evaluation history");
    const runs = await publishReports(wranglerStore(remote), reports);
    console.log(`verified ${runs.length} evaluation runs in ${remote ? "production" : "local Wrangler"} KV`);
  } finally { await rm(lock, { recursive: true }); }
}

if (import.meta.main) {
  const mode = process.argv[2];
  if ((mode !== "--local" && mode !== "--remote") || process.argv.length !== 3) throw new Error("choose --local or --remote explicitly");
  await upload(mode === "--remote");
}

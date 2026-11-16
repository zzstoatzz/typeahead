import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { publishReports, type SnapshotStore } from "../../src/eval/archive";
import { ROOT, readRuns } from "./archive";

export function directoryStore(root: string): SnapshotStore {
  const path = (key: string) => join(root, encodeURIComponent(key));
  return {
    async get(key) {
      try { return await readFile(path(key), "utf8"); }
      catch (error) {
        if (error instanceof Error && "code" in error && error.code === "ENOENT") return null;
        throw error;
      }
    },
    async put(key, value) {
      await mkdir(root, { recursive: true });
      const temp = `${path(key)}.${crypto.randomUUID()}.tmp`;
      await writeFile(temp, value, { flush: true });
      await rename(temp, path(key));
    },
  };
}

export async function publishLocal() {
  const reports = await readRuns();
  if (!reports.length) throw new Error("no evaluation history to publish");
  const runs = await publishReports(directoryStore(join(ROOT, "published")), reports);
  console.log(`prepared ${runs.length} runs in ${join(ROOT, "published")}`);
  return runs;
}

if (import.meta.main) await publishLocal();

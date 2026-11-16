import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import type { SnapshotStore } from "../../src/eval/archive";

export function wranglerStore(remote: boolean): SnapshotStore {
  const config = new URL("../../wrangler.jsonc", import.meta.url).pathname;
  const command = async (args: string[]) => {
    const child = Bun.spawn(["bun", "x", "wrangler", "kv", "key", ...args, "--binding=KV", `--config=${config}`, remote ? "--remote" : "--local"], { stdout: "pipe", stderr: "pipe" });
    const [stdout, stderr, exit] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
    if (exit !== 0) throw new Error(`Wrangler failed (${exit}): ${stderr}`);
    return stdout;
  };
  return {
    async get(key) {
      const keys = z.array(z.object({ name: z.string() })).parse(JSON.parse(await command(["list", `--prefix=${key}`])));
      if (!keys.some(k => k.name === key)) return null;
      return command(["get", key]);
    },
    async put(key, value) {
      const root = await mkdtemp(join(tmpdir(), "typeahead-eval-publish-"));
      try {
        const path = join(root, "value.json");
        await writeFile(path, value);
        await command(["put", key, `--path=${path}`]);
      } finally { await rm(root, { recursive: true }); }
    },
  };
}

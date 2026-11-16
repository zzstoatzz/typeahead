import { mkdir, readdir, readFile, writeFile, link, unlink } from "node:fs/promises";
import { join } from "node:path";
import { reportSchema, type Report } from "../../src/eval/model";

export const ROOT = process.env.EVAL_DATA_DIR ?? new URL("../../.eval/", import.meta.url).pathname;
export const SEEDS = new URL("../../data/eval/runs/", import.meta.url).pathname;

export async function archiveReport(report: Report, root = ROOT) {
  const dir = join(root, "runs");
  await mkdir(dir, { recursive: true });
  const path = join(dir, `${encodeURIComponent(report.run)}.json`);
  const body = JSON.stringify(reportSchema.parse(report), null, 2) + "\n";
  const temp = `${path}.${crypto.randomUUID()}.tmp`;
  await writeFile(temp, body, { flush: true });
  try {
    await link(temp, path);
  } catch (error) {
    if (!(error instanceof Error && "code" in error && error.code === "EEXIST")) throw error;
    if (await readFile(path, "utf8") !== body) throw new Error(`immutable archive conflict: ${report.run}`);
  } finally {
    await unlink(temp);
  }
}

export async function readRuns(root = ROOT, seeds = SEEDS): Promise<Report[]> {
  const byRun = new Map<string, Report>();
  for (const dir of [seeds, join(root, "runs")]) {
    const files = await readdir(dir).catch(error => {
      if (error instanceof Error && "code" in error && error.code === "ENOENT") return [];
      throw error;
    });
    for (const file of files.filter(f => f.endsWith(".json"))) {
      const report = reportSchema.parse(JSON.parse(await readFile(join(dir, file), "utf8")));
      const previous = byRun.get(report.run);
      if (previous && JSON.stringify(previous) !== JSON.stringify(report)) throw new Error(`conflicting copies of ${report.run}`);
      byRun.set(report.run, report);
    }
  }
  return [...byRun.values()].sort((a, b) => b.startedAt - a.startedAt);
}


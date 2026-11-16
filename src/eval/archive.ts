import { z } from "zod";
import { reportSchema, type Report } from "./model";

export const INDEX_KEY = "evaluation:index:v1";
export const HISTORY_LIMIT = 365;
export const entrySchema = z.object({
  run: z.string(), corpus: z.string(), location: z.string(), startedAt: z.number(),
  completed: z.number().int().nonnegative(), expected: z.number().int().positive(),
  hash: z.string().regex(/^[a-f0-9]{64}$/),
});
export type RunEntry = z.infer<typeof entrySchema>;
export const indexSchema = z.object({ version: z.literal(1), runs: z.array(entrySchema).max(HISTORY_LIMIT) })
  .refine(index => new Set(index.runs.map(r => r.run)).size === index.runs.length, "duplicate run");
export const reportKey = (hash: string) => `evaluation:run:${hash}`;

export async function digest(body: string): Promise<string> {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body)))].map(b => b.toString(16).padStart(2, "0")).join("");
}

export async function entryFor(report: Report): Promise<RunEntry> {
  return { run: report.run, corpus: report.corpus, location: report.location, startedAt: report.startedAt,
    completed: report.observations.filter(o => o.outcome !== "pending").length, expected: report.expected, hash: await digest(JSON.stringify(report)) };
}

export interface SnapshotStore {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
}

export async function publishReports(store: SnapshotStore, reports: Report[]): Promise<RunEntry[]> {
  const raw = await store.get(INDEX_KEY);
  const existing = raw === null ? [] : indexSchema.parse(JSON.parse(raw)).runs;
  const entries = new Map(existing.map(e => [e.run, e]));
  for (const input of reports) {
    const report = reportSchema.parse(input);
    const entry = await entryFor(report);
    const previous = entries.get(entry.run);
    if (previous && previous.hash !== entry.hash) throw new Error(`refusing to replace immutable run ${entry.run}`);
    if (!previous) {
      const key = reportKey(entry.hash);
      const body = JSON.stringify(report);
      await store.put(key, body);
      if (await store.get(key) !== body) throw new Error(`snapshot readback failed: ${entry.run}; index not updated`);
      entries.set(entry.run, entry);
    }
  }
  const runs = [...entries.values()].sort((a, b) => b.startedAt - a.startedAt).slice(0, HISTORY_LIMIT);
  const body = JSON.stringify({ version: 1, runs });
  if (body !== raw) {
    await store.put(INDEX_KEY, body);
    if (await store.get(INDEX_KEY) !== body) throw new Error("index readback failed");
  }
  return runs;
}

export async function readSnapshot(store: Pick<SnapshotStore, "get">, run: string | null) {
  const raw = await store.get(INDEX_KEY);
  const runs = raw === null ? [] : indexSchema.parse(JSON.parse(raw)).runs;
  const selected = run === null ? runs[0] : runs.find(r => r.run === run);
  if (!selected) return { report: null, runs };
  const body = await store.get(reportKey(selected.hash));
  if (body === null || await digest(body) !== selected.hash) throw new Error("evaluation snapshot unavailable or checksum mismatch");
  const report = reportSchema.parse(JSON.parse(body));
  if (report.run !== selected.run) throw new Error("evaluation run mismatch");
  return { report, runs };
}

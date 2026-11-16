import { appendFile, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { hostname } from "node:os";
import { nextAllowed, observationSchema, reportSchema, type Observation, type Report } from "../../src/eval/model";
import { newRun, nextRunAt, reservation, sample } from "../../src/eval/sampling";
import { ROOT, archiveReport, readRuns } from "./archive";

export async function collect({ scheduled = false } = {}) {
  await mkdir(ROOT, { recursive: true });
  const lock = join(ROOT, "collector.lock");
  await mkdir(lock).catch(() => { throw new Error(`collector already locked: ${lock}; inspect the owner before removing it`); });
  await writeFile(join(lock, "owner.json"), JSON.stringify({ pid: process.pid, host: hostname(), at: new Date().toISOString() }));
  const ledger = join(ROOT, "requests.jsonl");
  let report: Report | undefined;
  try {
    let runs = await readRuns();
    if (await Bun.file(join(ROOT, "latest.json")).exists()) {
      const interrupted = reportSchema.parse(JSON.parse(await readFile(join(ROOT, "latest.json"), "utf8")));
      if (!runs.some(r => r.run === interrupted.run)) {
        await archiveReport(interrupted);
        runs = await readRuns();
      }
    }
    const previous = await Bun.file(ledger).exists() ? await readFile(ledger, "utf8") : "";
    const indexed = new Map<string, Observation>();
    for (const line of previous.split("\n").filter(Boolean)) { const o = observationSchema.parse(JSON.parse(line)); indexed.set(o.id, o); }
    for (const run of runs) for (const o of run.observations) if (!indexed.has(o.id)) indexed.set(o.id, o);
    const lastStart = [...indexed.values()].reduce<number | null>((last, o) => Math.max(last ?? 0, Date.parse(o.run) || o.startedAt), runs[0]?.startedAt ?? null);
    if (scheduled && Date.now() < nextRunAt(lastStart)) { console.log(`not due until ${new Date(nextRunAt(lastStart)).toISOString()}`); return; }
    const current = newRun(Date.now(), process.env.EVAL_LOCATION ?? "local laptop");
    report = current;
    const save = async (o: Observation) => {
      await appendFile(ledger, JSON.stringify(o) + "\n", { flush: true });
      indexed.set(o.id, o);
      current.observations = [...indexed.values()].filter(value => value.run === current.run);
      current.updatedAt = Date.now();
      await writeFile(join(ROOT, "latest.tmp"), JSON.stringify(current, null, 2));
      await rename(join(ROOT, "latest.tmp"), join(ROOT, "latest.json"));
    };
    while (true) {
      const next = reservation(current, Date.now());
      if (!next) return;
      const now = Date.now();
      const due = nextAllowed([...indexed.values()], next.provider, now);
      const wait = due - now;
      if (wait > 30_000) {
        console.log(`stopping: ${next.provider} budget or cooldown until ${new Date(due).toISOString()}`);
        return;
      }
      if (wait > 0) await Bun.sleep(wait);
      const attempt = { ...next, startedAt: Date.now() };
      await save(attempt);
      const result = await sample(attempt, fetch);
      await save(result);
      console.log(`${current.observations.length}/${current.expected} ${next.provider} ${JSON.stringify(next.q)} ${result.outcome} ${result.ms?.toFixed(0)} ms`);
      if (result.cooldownUntil > Date.now()) return;
    }
  } finally {
    try { if (report?.observations.length) await archiveReport(report); }
    finally { await rm(lock, { recursive: true }); }
  }
}

if (import.meta.main) await collect();

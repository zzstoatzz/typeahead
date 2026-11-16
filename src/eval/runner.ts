import { publishReports, readSnapshot, type SnapshotStore } from "./archive";
import { GAP_MS, nextAllowed, reportSchema, type AttemptTiming, type Observation, type Report } from "./model";
import { newRun, nextRunAt, reservation, sample } from "./sampling";

type ActiveRun = {
  report: Report;
  previous: AttemptTiming[];
  phase: "collect" | "publish" | "verify";
};
interface Journal extends Pick<DurableObjectStorage, "setAlarm" | "getAlarm"> {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
  list<T>(options: { prefix: string }): Promise<Map<string, T>>;
  delete(keys: string[]): Promise<number>;
}
const SITE = "https://typeahead.waow.tech";
const ATTEMPTS = "attempt:";
const attemptKey = (i: number) => ATTEMPTS + String(i).padStart(3, "0");

export class EvalRunner {
  private busy = false;

  constructor(
    private readonly state: Journal,
    private readonly snapshots: SnapshotStore,
    private readonly request: (url: string, init: RequestInit) => Promise<Response>,
    private readonly clock = Date.now,
  ) {}

  async wake() {
    if (!this.busy && await this.state.getAlarm() === null && !this.busy) {
      await this.state.setAlarm(this.clock());
    }
  }

  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      const active = await this.state.get<ActiveRun>("active") ?? await this.begin();
      if (!active) return;
      const attempts = await this.state.list<Observation>({ prefix: ATTEMPTS });
      const observations = [...attempts.values()];
      const latest = observations.at(-1);
      const report = { ...active.report, observations, updatedAt: latest?.finishedAt ?? latest?.startedAt ?? active.report.updatedAt };
      if (active.phase === "collect") {
        const next = reservation(report, this.clock());
        if (next && latest?.outcome !== "pending" && (!latest || latest.cooldownUntil === 0)) {
          const now = this.clock();
          const due = nextAllowed([...active.previous, ...observations], next.provider, now);
          if (due <= now) {
            const key = attemptKey(observations.length);
            await this.state.put(key, next);
            const result = await sample(next, this.request);
            await this.state.put(key, result);
            await this.state.setAlarm(this.clock() + (result.cooldownUntil > 0 ? 0 : GAP_MS));
            return;
          }
          if (!latest || due - now <= 30_000) {
            await this.state.setAlarm(due);
            return;
          }
        }
        active.phase = "publish";
        await this.state.put("active", active);
      }
      if (active.phase === "publish") {
        await publishReports(this.snapshots, [report]);
        active.phase = "verify";
        await this.state.put("active", active);
        console.log({ event: "eval_published", run: report.run, observations: observations.length, expected: report.expected });
        await this.state.setAlarm(this.clock() + 65_000);
        return;
      }
      await this.verify(report);
      await this.state.put("lastStart", report.startedAt);
      await this.state.delete([...attempts.keys(), "active"]);
    } finally {
      this.busy = false;
    }
  }

  private async begin(): Promise<ActiveRun | undefined> {
    const lastStart = await this.state.get<number>("lastStart") ?? null;
    if (this.clock() < nextRunAt(lastStart)) return;
    const { report: previous } = await readSnapshot(this.snapshots, null);
    if (!previous) throw new Error("seed evaluation must be present before scheduling");
    const due = nextRunAt(previous.startedAt);
    if (this.clock() < due) {
      await this.verify(previous);
      await this.state.put("lastStart", previous.startedAt);
      console.log({ event: "eval_not_due", eligibleAt: new Date(due).toISOString() });
      return;
    }
    const report = newRun(this.clock(), "Cloudflare Workers (ENAM placement hint)");
    const active: ActiveRun = {
      report,
      previous: previous.observations.map(({ provider, startedAt, finishedAt, cooldownUntil }) =>
        ({ provider, startedAt, finishedAt, cooldownUntil })),
      phase: "collect",
    };
    await this.state.put("active", active);
    return active;
  }

  private async verify(report: Report) {
    const raw = await this.request(SITE + "/eval/data.json", { signal: AbortSignal.timeout(10_000) });
    if (!raw.ok) throw new Error(`eval data readback returned ${raw.status}`);
    const snapshot = reportSchema.safeParse(await raw.json());
    if (!snapshot.success || JSON.stringify(snapshot.data) !== JSON.stringify(reportSchema.parse(report))) {
      throw new Error(`latest public eval snapshot does not match ${report.run}`);
    }
    const page = await this.request(SITE + "/eval", { signal: AbortSignal.timeout(10_000) });
    if (!page.ok || !(await page.text()).includes(`/eval/data.json?run=${encodeURIComponent(report.run)}`)) {
      throw new Error(`latest public eval page does not show ${report.run}`);
    }
    console.log({ event: "eval_visible", run: report.run, observations: report.observations.length });
  }
}

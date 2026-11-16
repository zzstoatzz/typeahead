import { afterEach, expect, setSystemTime, test } from "bun:test";
import { publishReports, readSnapshot, type SnapshotStore } from "./archive";
import { GAP_MS } from "./model";
import { EvalRunner } from "./runner";
import { newRun } from "./sampling";

afterEach(() => setSystemTime());

function fixture(manualRequest?: string) {
  const entries = new Map<string, unknown>();
  const records = new Map<string, string>();
  let alarm: number | null = null;
  let writesInterrupted = false;
  const state = {
    async get<T>(key: string) {
      // SAFETY: the runner is the only writer and reads each key with its own stored type.
      return structuredClone(entries.get(key)) as T | undefined;
    },
    async put<T>(key: string, value: T) {
      if (writesInterrupted) throw new Error("storage interrupted after fetch");
      entries.set(key, structuredClone(value));
    },
    async list<T>({ prefix }: { prefix: string }) {
      // SAFETY: the only listed prefix contains the runner's stored observations.
      return new Map([...entries.entries()].filter(([key]) => key.startsWith(prefix)).sort(([a], [b]) => a.localeCompare(b))) as Map<string, T>;
    },
    async delete(keys: string[]) { return keys.filter(key => entries.delete(key)).length; },
    async setAlarm(at: number) { alarm = at; },
    async getAlarm() { return alarm; },
  };
  const snapshots: SnapshotStore = {
    async get(key) { return records.get(key) ?? null; },
    async put(key, value) { records.set(key, value); },
  };
  let failure: Response | undefined;
  let verifyStale = false;
  let interruptAfterFetch = false;
  const samples: { host: string; at: number }[] = [];
  const request = async (input: string) => {
    const url = new URL(input);
    const { report } = await readSnapshot(snapshots, null);
    if (url.pathname === "/eval/data.json") return Response.json(verifyStale ? null : report);
    if (url.pathname === "/eval") return new Response("/eval/data.json?run=" + encodeURIComponent(report?.run ?? ""));
    samples.push({ host: url.hostname, at: Date.now() });
    writesInterrupted = interruptAfterFetch;
    return failure ?? Response.json({ actors: [] });
  };
  let runner = new EvalRunner(state, snapshots, request, Date.now, manualRequest);
  return {
    snapshots, samples,
    fail: (response: Response) => { failure = response; },
    interrupt: (enabled: boolean) => { interruptAfterFetch = enabled; writesInterrupted = false; },
    stale: (stale: boolean) => { verifyStale = stale; },
    async bootstrap(at: number) { setSystemTime(at); await runner.wake(); },
    async runAlarm() {
      if (alarm === null) throw new Error("no alarm");
      setSystemTime(alarm);
      alarm = null;
      runner = new EvalRunner(state, snapshots, request, Date.now, manualRequest);
      await runner.tick();
    },
    alarm: () => alarm,
  };
}

test("the seed gates collection and a daily run survives eviction, follows the request gap, and verifies the published page", async () => {
  const f = fixture();
  const seed = newRun(Date.UTC(2026, 10, 16), "seed");
  await publishReports(f.snapshots, [seed]);
  const due = seed.startedAt + 86_400_000;
  await f.bootstrap(due - 1);
  await f.runAlarm();
  expect(f.samples).toHaveLength(0);

  await f.bootstrap(due);
  for (let i = 0; i < 81; i++) await f.runAlarm();
  expect(f.samples).toHaveLength(80);
  expect(f.samples.filter(s => s.host === "public.api.bsky.app")).toHaveLength(40);
  expect(f.samples.slice(1).every((s, i) => s.at - f.samples[i].at >= GAP_MS)).toBe(true);
  const { report, runs } = await readSnapshot(f.snapshots, null);
  expect(runs).toHaveLength(2);
  expect(report?.observations.every(o => o.outcome === "ok")).toBe(true);

  f.stale(true);
  await expect(f.runAlarm()).rejects.toThrow("latest public eval snapshot does not match");
  f.stale(false);
  await f.bootstrap(due + 20 * 60_000);
  await f.runAlarm();
  expect(f.alarm()).toBeNull();
  expect(f.samples).toHaveLength(80);
});

test("a Bluesky 429 archives the partial run and does not retry collection", async () => {
  const f = fixture();
  const seed = newRun(Date.UTC(2026, 10, 16), "seed");
  await publishReports(f.snapshots, [seed]);
  const due = seed.startedAt + 86_400_000;
  await f.bootstrap(due);
  await f.runAlarm();
  f.fail(new Response(null, { status: 429, headers: { "Retry-After": "7200" } }));
  await f.runAlarm();
  await f.runAlarm();
  await f.runAlarm();
  await f.bootstrap(due + 15 * 60_000);
  await f.runAlarm();
  expect(f.samples.map(s => s.host)).toEqual(["typeahead.waow.tech", "public.api.bsky.app"]);
  const { report } = await readSnapshot(f.snapshots, null);
  expect(report?.observations.map(o => o.status)).toEqual([200, 429]);
  expect(report?.observations.at(-1)?.cooldownUntil).toBe(due + GAP_MS + 7_200_000);
});

test("a crash after reserving an attempt never causes the query to be sent twice", async () => {
  const f = fixture();
  const seed = newRun(Date.UTC(2026, 10, 16), "seed");
  await publishReports(f.snapshots, [seed]);
  const due = seed.startedAt + 86_400_000;
  await f.bootstrap(due);
  f.interrupt(true);
  await expect(f.runAlarm()).rejects.toThrow("storage interrupted after fetch");

  f.interrupt(false);
  await f.bootstrap(due + 15 * 60_000);
  await f.runAlarm();
  await f.runAlarm();
  expect(f.samples).toHaveLength(1);
  const { report } = await readSnapshot(f.snapshots, null);
  expect(report?.observations.map(o => o.outcome)).toEqual(["pending"]);
});

test("an explicit manual request runs once before the daily gate and survives object eviction", async () => {
  const f = fixture("requested-run");
  const seed = newRun(Date.UTC(2026, 10, 16), "seed");
  await publishReports(f.snapshots, [seed]);
  const now = seed.startedAt + 60_000;
  await f.bootstrap(now);
  for (let i = 0; i < 82; i++) await f.runAlarm();
  expect(f.samples).toHaveLength(80);
  expect((await readSnapshot(f.snapshots, null)).report?.location).toContain("Cloudflare Workers");
  await f.bootstrap(now + 3_600_000);
  await f.runAlarm();
  expect(f.samples).toHaveLength(80);
  expect(f.alarm()).toBeNull();
});

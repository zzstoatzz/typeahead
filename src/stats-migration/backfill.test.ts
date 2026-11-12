import { afterEach, beforeEach, expect, setDefaultTimeout, setSystemTime, test } from "bun:test";
import { materializeStats, readRequestPayload, readStatsPayload } from "../enrichment";
import { handleStatsBackfill } from "./handlers";
import { BACKFILL_STEPS, BackfillInputError, backfillStep, runBackfill, type BackfillRequest, type BackfillResult } from "./backfill";
import { CUT, NOW, T0, backfilled, seed } from "./backfill-fixture";
import { statsRows, statsStores } from "../stats-d1-fixture";
import { readProfile } from "../stats-profile";
import type { Env } from "../types";

// each test seeds two real stores, one of them a workerd process
setDefaultTimeout(60_000);

// The history copy from Turso into D1 (docs/plans/stats-off-turso.md, step 3).
// Both stores counted across the start of dual write and across the cut; the
// copy must leave D1 with Turso's history, D1's own count from the cut on,
// and totals that count nothing twice.

let stores: Awaited<ReturnType<typeof statsStores>>;

beforeEach(async () => {
  stores = await statsStores();
  setSystemTime(new Date(NOW));
});

afterEach(async () => {
  setSystemTime();
  await stores.dispose();
});

const call = (request: BackfillRequest) => backfillStep(stores.turso, stores.mirror, request);
const d1Rows = async () => Object.fromEntries((await statsRows(stores.mirror)).map((entry) => [entry.table, entry.rows]));
const apply = { cut: CUT, dry_run: false, limit: 60 };

test("a dry run reports what differs and writes nothing", async () => {
  await seed(stores.turso, stores.mirror, { agree: false });
  const before = await d1Rows();
  const run = await runBackfill(call, { ...apply, dry_run: true });
  expect(await d1Rows()).toEqual(before);
  expect(run.changes).toBeGreaterThan(1000);
  expect(Object.values(run.steps).map((step) => step.wrote)).toEqual([0, 0, 0, 0, 0, 0, 0, 0]);
  expect(Object.values(run.steps).every((step) => step.changes > 0)).toBe(true);
});

test("D1 ends with Turso's rows before the cut, its own from it, and totals that count nothing twice", async () => {
  const seeded = await seed(stores.turso, stores.mirror, { agree: false });
  const results: BackfillResult[] = [];
  const run = await runBackfill(call, apply, (result) => results.push(result));
  expect(run.calls).toBeGreaterThan(20);
  expect(await d1Rows()).toEqual(backfilled(seeded));

  // the label cursor in Turso's counters table is not a statistic
  expect((await d1Rows()).counters).toHaveLength(1);
  // rows D1 held that Turso does not are gone before the cut and kept after it
  const sources = (await d1Rows()).traffic_sources!.map((row) => row.domain);
  expect(sources).not.toContain("d1-only-before-cut.example");
  expect(sources).toContain("d1-only-since-cut.example");
  expect(sources).toContain("old-app.example");

  const totals = results.at(-1)!.report;
  expect(totals.kind).toBe("totals");
  if (totals.kind === "totals") {
    expect(totals.actors_total.target).toBe(totals.actors_total.turso - totals.actors_total.turso_since_cut + totals.actors_total.d1_since_cut);
    expect(totals.actors_total.d1_since_cut).not.toBe(totals.actors_total.turso_since_cut);
  }
});

test("the totals are Turso's at the cut plus D1's count since, on numbers small enough to check by hand", async () => {
  const insert = (db: typeof stores.turso, table: string, columns: string, values: string) => db.prepare(`INSERT INTO ${table} (${columns}) VALUES ${values}`).run();
  // Turso: 1000 hits all time, 30 of them since the cut. D1 counted 28 since
  // the cut (it dropped two) and 40 between the start of dual write and the cut.
  await insert(stores.turso, "traffic_sources", "domain, hits", "('plyr.fm', 1000)");
  await insert(stores.turso, "traffic_buckets", "bucket, domain, hits", `(${CUT - 9}, 'plyr.fm', 41), (${CUT}, 'plyr.fm', 30)`);
  await insert(stores.mirror, "traffic_sources", "domain, hits", "('plyr.fm', 68)");
  await insert(stores.mirror, "traffic_buckets", "bucket, domain, hits", `(${CUT - 9}, 'plyr.fm', 40), (${CUT + 1}, 'plyr.fm', 28)`);
  // Turso: 13,000,000 actors, 12 of them added since the cut. D1 saw 15 since.
  await stores.turso.prepare("UPDATE counters SET value = 13000000 WHERE name = 'actors_total'").run();
  await insert(stores.turso, "actor_deltas", "bucket, actors_delta", `(${CUT - 9}, 7), (${CUT}, 12)`);
  await stores.mirror.prepare("UPDATE counters SET value = 21 WHERE name = 'actors_total'").run();
  await insert(stores.mirror, "actor_deltas", "bucket, actors_delta", `(${CUT - 9}, 6), (${CUT}, 15)`);

  await runBackfill(call, apply);
  const rows = await d1Rows();
  expect(rows.traffic_sources).toEqual([{ domain: "plyr.fm", hits: 1000 - 30 + 28 }]);
  expect(rows.counters).toEqual([{ name: "actors_total", value: 13_000_000 - 12 + 15 }]);
  expect(rows.traffic_buckets).toEqual([{ bucket: CUT - 9, domain: "plyr.fm", hits: 41 }, { bucket: CUT + 1, domain: "plyr.fm", hits: 28 }]);
  expect(rows.actor_deltas).toMatchObject([{ bucket: CUT - 9, actors_delta: 7 }, { bucket: CUT, actors_delta: 15 }]);
});

test("a second full run changes nothing and writes nothing", async () => {
  const seeded = await seed(stores.turso, stores.mirror, { agree: false });
  await runBackfill(call, apply);
  const second = await runBackfill(call, apply);
  expect(second.changes).toBe(0);
  expect(Object.values(second.steps).map((step) => step.wrote)).toEqual([0, 0, 0, 0, 0, 0, 0, 0]);
  expect(await d1Rows()).toEqual(backfilled(seeded));
});

test("a run that stops part way finishes from where it stopped, or from the start", async () => {
  for (const resume of ["from the last position", "from the start"]) {
    await stores.dispose();
    stores = await statsStores();
    const seeded = await seed(stores.turso, stores.mirror, { agree: false });
    let calls = 0;
    let position: BackfillResult["next"] = null;
    const dropping = async (request: BackfillRequest) => {
      if (++calls > 13) throw new Error("connection lost");
      const result = await call(request);
      position = result.next;
      return result;
    };
    await expect(runBackfill(dropping, apply)).rejects.toThrow("connection lost");
    expect(position).toMatchObject({ step: "traffic_buckets" });
    expect(await d1Rows()).not.toEqual(backfilled(seeded));

    await runBackfill(call, { ...apply, start: resume === "from the start" ? undefined : position ?? undefined });
    expect(await d1Rows()).toEqual(backfilled(seeded));
  }
});

test("a rollup on Turso between two halves of a run leaves both stores telling the cron the same thing", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  let position: BackfillResult["next"] = null;
  const untilDaily = async (request: BackfillRequest) => {
    if (request.step === "traffic_daily") throw new Error("stopped before the daily tables");
    const result = await call(request);
    position = result.next;
    return result;
  };
  await expect(runBackfill(untilDaily, apply)).rejects.toThrow("stopped before the daily tables");

  // five days on, the hourly cron has moved Turso's oldest buckets into its daily tables
  const later = NOW + 5 * 86_400_000;
  setSystemTime(new Date(later));
  const before = await stores.turso.prepare("SELECT count(*) AS n FROM traffic_buckets").first<{ n: number }>();
  await materializeStats(stores.turso, { KV: { async put() {} } });
  const after = await stores.turso.prepare("SELECT count(*) AS n FROM traffic_buckets").first<{ n: number }>();
  expect(after!.n).toBeLessThan(before!.n);

  await runBackfill(call, { ...apply, start: position ?? undefined });
  expect(await readStatsPayload(stores.mirror, later)).toEqual(await readStatsPayload(stores.turso, later));
  expect(await readRequestPayload(stores.mirror, later)).toEqual(await readRequestPayload(stores.turso, later));
  expect(await readProfile(stores.mirror, later)).toEqual(await readProfile(stores.turso, later));
  expect((await d1Rows()).traffic_buckets).toHaveLength(after!.n);
});

test("a later cut replaces what D1 counted alone up to it", async () => {
  const seeded = await seed(stores.turso, stores.mirror, { agree: false });
  await runBackfill(call, apply);
  await runBackfill(call, { ...apply, cut: CUT + 2 });
  expect(await d1Rows()).toEqual(backfilled(seeded, CUT + 2));
});

test("a cut before dual write started, or one that is still open, is refused", async () => {
  const request = { step: BACKFILL_STEPS[0], cursor: 0, limit: 60, dry_run: false };
  await expect(call({ ...request, cut: T0 - 1 })).rejects.toBeInstanceOf(BackfillInputError);
  const current = Math.floor(NOW / 300_000);
  await expect(call({ ...request, cut: current })).rejects.toBeInstanceOf(BackfillInputError);
  expect((await call({ ...request, cut: current - 1 })).cut_utc).toBe(new Date((current - 1) * 300_000).toISOString());
});

test("the endpoint needs the admin secret, a complete request and an explicit dry_run", async () => {
  const fields: Partial<Env> = { ADMIN_SECRET: "s3cret", STATS_DB: stores.d1, RATE_LIMITER_STRICT: { limit: async () => ({ success: true }) } };
  // SAFETY: the handler reads ADMIN_SECRET, STATS_DB and, on a refused
  // request, the strict rate limiter.
  const env = fields as Env;
  const post = (body: Partial<BackfillRequest>, secret = "s3cret") => handleStatsBackfill(new Request("https://typeahead.waow.tech/admin/stats-backfill", {
    method: "POST", headers: { Authorization: `Bearer ${secret}` }, body: JSON.stringify(body),
  }), stores.turso, env);
  const request = { step: "metrics", cursor: 0, cut: CUT, limit: 60 } as const;

  expect((await post({ ...request, dry_run: true }, "wrong")).status).toBe(401);
  expect((await post(request)).status).toBe(400);
  expect((await post({ ...request, dry_run: true, cut: T0 - 1 })).status).toBe(400);
  expect((await handleStatsBackfill(new Request("https://typeahead.waow.tech/admin/stats-backfill", {
    method: "POST", headers: { Authorization: "Bearer s3cret" }, body: JSON.stringify({ ...request, dry_run: true }),
  }), stores.turso, { ...env, STATS_DB: undefined })).status).toBe(503);

  await seed(stores.turso, stores.mirror, { agree: false });
  const ok = await post({ ...request, dry_run: true });
  expect(ok.status).toBe(200);
  expect(await ok.json<BackfillResult>()).toMatchObject({ step: "metrics", dry_run: true, wrote: false, next: { step: "metrics", cursor: expect.any(Number) } });
});

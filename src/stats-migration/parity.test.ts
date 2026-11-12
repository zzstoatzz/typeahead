import { afterEach, beforeEach, expect, setDefaultTimeout, setSystemTime, test } from "bun:test";
import { materializeStats } from "../enrichment";
import { handleStatsParity } from "./handlers";
import { runBackfill, backfillStep, type BackfillRequest } from "./backfill";
import { CUT, NOW, T0, seed } from "./backfill-fixture";
import { statsStores } from "../stats-d1-fixture";
import { PARITY_SECTIONS, ParityInputError, parityStep, runParity, type ParityReport, type ParityRequest, type ParityResult } from "./parity";
import { RANGED_NAMES } from "./rows";
import type { Env } from "../types";

// each test seeds two real stores, one of them a workerd process
setDefaultTimeout(60_000);

// The comparison that gates reading from D1 (docs/plans/stats-off-turso.md,
// steps 4 and 5): after a backfill the two stores must be reported equal, and
// a difference planted in either one must be reported where it is.

let stores: Awaited<ReturnType<typeof statsStores>>;

beforeEach(async () => {
  stores = await statsStores();
  setSystemTime(new Date(NOW));
});

afterEach(async () => {
  setSystemTime();
  await stores.dispose();
});

const backfill = (request: BackfillRequest) => backfillStep(stores.turso, stores.mirror, request);
const compare = (request: ParityRequest) => parityStep(stores.turso, stores.mirror, request);
const everything: Parameters<typeof runParity>[1] = { from: 0, limit: 150, sections: PARITY_SECTIONS };
const CLOSED = Math.floor(NOW / 300_000) - 1;

async function reports(options = everything) {
  const results: ParityResult[] = [];
  const run = await runParity(compare, options, (result) => results.push(result));
  const report = <K extends ParityReport["kind"]>(section: string, kind: K) => {
    const found = results.filter((result) => result.section === section).map((result) => result.report);
    return found.filter((entry): entry is Extract<ParityReport, { kind: K }> => entry.kind === kind);
  };
  return { run, results, report };
}

test("stores that agree after a backfill are reported equal in every section", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  await runBackfill(backfill, { cut: CUT, dry_run: false, limit: 500 });
  const { run, results } = await reports();
  expect(run.differences).toBe(0);
  expect(run.open_differences).toBe(0);
  expect(run.closed_before).toBe(CLOSED);
  expect(Object.keys(run.sections)).toEqual([...PARITY_SECTIONS]);
  expect(Object.values(run.sections).every((section) => section.compared > 0)).toBe(true);
  expect(run.sections.request_buckets!.calls).toBeGreaterThan(1);
  expect(results.every((result) => result.closed_before === CLOSED)).toBe(true);
});

test("before a backfill only the buckets since dual write started can be compared, and D1's partial first bucket shows", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  const { run } = await reports({ from: T0, limit: 150, sections: RANGED_NAMES });
  expect(run.differences).toBe(0);
  const all = await reports({ from: T0 - 1, limit: 150, sections: RANGED_NAMES });
  expect(all.run.sections.traffic_buckets!.differences).toBe(1);
  expect(all.report("traffic_buckets", "rows").flatMap((report) => report.diff.examples)).toEqual([
    { turso: { bucket: T0 - 1, domain: "leaflet.pub", hits: 1 + ((T0 - 1) % 5) }, d1: { bucket: T0 - 1, domain: "leaflet.pub", hits: 1 } },
  ]);
});

test("a planted difference is reported in the section and row that hold it", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  await runBackfill(backfill, { cut: CUT, dry_run: false, limit: 500 });
  const sql = (statement: string, ...args: (string | number)[]) => stores.mirror.prepare(statement).bind(...args).run();

  // a dropped write, a row Turso lacks, a row D1 lacks, a real off in its last place, a stale total
  const closed = (await stores.mirror.prepare("SELECT bucket, domain, hits FROM traffic_buckets WHERE bucket = ?1 ORDER BY domain LIMIT 1").bind(CUT + 1).first<{ bucket: number; domain: string; hits: number }>())!;
  await sql("UPDATE traffic_buckets SET hits = hits - 1 WHERE bucket = ?1 AND domain = ?2", closed.bucket, closed.domain);
  await sql("INSERT INTO request_buckets (bucket, domain, endpoint, status, hits) VALUES (?1, 'only-in-d1.example', 'search', 429, 6)", CUT - 40);
  await sql("DELETE FROM actor_deltas WHERE bucket = ?1", T0);
  await sql("UPDATE metrics SET total_ms = total_ms + 0.000001 WHERE hour = ?1", CUT - 1);
  await sql("UPDATE snapshots SET with_avatars = with_avatars + 1 WHERE hour = ?1", Math.floor(T0 / 12) - 100);
  await sql("UPDATE traffic_daily SET hits = hits + 3 WHERE day = (SELECT min(day) FROM traffic_daily) AND domain = 'plyr.fm'");
  await sql("UPDATE counters SET value = value - 9 WHERE name = 'actors_total'");
  await sql("UPDATE traffic_sources SET hits = hits + 11 WHERE domain = 'old-app.example'");

  const { run, report } = await reports();
  const rows = (table: string) => report(table, "rows").flatMap((entry) => entry.diff.examples);

  expect(rows("traffic_buckets")).toEqual([{ turso: closed, d1: { ...closed, hits: closed.hits - 1 } }]);
  expect(rows("request_buckets")).toEqual([{ turso: null, d1: { bucket: CUT - 40, domain: "only-in-d1.example", endpoint: "search", status: 429, hits: 6 } }]);
  expect(rows("actor_deltas")).toMatchObject([{ turso: { bucket: T0 }, d1: null }]);
  expect(rows("metrics")).toHaveLength(1);
  expect(rows("metrics")[0]!.turso!.hour).toBe(CUT - 1);
  expect(Number(rows("metrics")[0]!.d1!.total_ms) - Number(rows("metrics")[0]!.turso!.total_ms)).toBeCloseTo(0.000001, 9);
  expect(rows("snapshots")).toMatchObject([{ turso: { hour: Math.floor(T0 / 12) - 100, with_avatars: 9_000_000 }, d1: { with_avatars: 9_000_001 } }]);

  const daily = report("daily", "daily")[0]!;
  // the edited daily row, and the day of the dropped traffic write
  expect(daily.tables.traffic_daily).toMatchObject({ differing: 2, only_turso: 0, only_d1: 0 });
  expect(daily.tables.traffic_daily.examples).toMatchObject([
    { turso: { domain: "plyr.fm", hits: 102 }, d1: { hits: 105 } },
    { turso: { day: Math.floor(closed.bucket / 288), domain: closed.domain }, d1: {} },
  ]);
  expect(daily.tables.request_daily).toMatchObject({ differing: 0, only_turso: 0, only_d1: 1 });

  const totals = report("totals", "totals")[0]!;
  expect(totals.traffic_sources.examples).toHaveLength(1);
  expect(totals.traffic_sources.examples[0]).toMatchObject({ turso: { domain: "old-app.example", hits: 25 }, d1: { hits: 36 } });
  expect(totals.actors_total.turso! - totals.actors_total.d1!).toBe(9);

  const stats = report("payload_stats", "payload")[0]!;
  expect(stats.series.metrics!.closed.differing).toBe(1);
  expect(stats.series.snapshots!.closed.differing).toBe(1);
  expect(stats.series.deltas!.closed).toMatchObject({ differing: 0, only_turso: 1, only_d1: 0 });
  expect(stats.series.traffic_series_5m!.closed.differing).toBe(1);
  // the hour and the day of the dropped write are still being written
  expect(stats.series.traffic_series!).toMatchObject({ closed: { differing: 0 }, open: { differing: 1 } });
  expect(stats.series.traffic_daily!).toMatchObject({ closed: { differing: 1 }, open: { differing: 1 } });
  expect(stats.scalars.card_searches!.turso).toBe(stats.scalars.card_searches!.d1);
  const requests = report("payload_requests", "payload")[0]!;
  expect(requests.series.series_5m!.closed.only_d1).toBe(1);
  expect(requests.series.daily!).toMatchObject({ closed: { only_d1: 0 }, open: { only_d1: 1 } });

  expect(run.sections.totals!.differences).toBe(2);
  expect(run.sections.payload_profile!.differences).toBe(0);
  expect(run.differences).toBeGreaterThan(12);
});

test("a difference in the profile's 28-day window is reported by band", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  await runBackfill(backfill, { cut: CUT, dry_run: false, limit: 500 });
  // the bands are quantiles, so one odd bucket moves none of them: change every bucket in the window
  await stores.mirror.prepare("UPDATE traffic_buckets SET hits = hits + 5000 WHERE bucket < ?1").bind(CLOSED - 2 * 288).run();
  const { report } = await reports({ ...everything, sections: ["payload_profile"] });
  const bands = report("payload_profile", "profile")[0]!.differing_bands;
  expect(bands.length).toBeGreaterThan(0);
  expect(bands.every((band) => band.startsWith("source."))).toBe(true);
  expect(bands.some((band) => band.startsWith("source.h["))).toBe(true);
});

test("a write that has reached one store only is an open difference, not a closed one", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  await runBackfill(backfill, { cut: CUT, dry_run: false, limit: 500 });
  const open = Math.floor(NOW / 300_000);
  await stores.turso.batch([
    stores.turso.prepare("INSERT INTO traffic_buckets (bucket, domain, hits) VALUES (?1, 'plyr.fm', 1)").bind(open),
    stores.turso.prepare("UPDATE traffic_sources SET hits = hits + 1 WHERE domain = 'plyr.fm'"),
    stores.turso.prepare("INSERT INTO request_buckets (bucket, domain, endpoint, status, hits) VALUES (?1, 'plyr.fm', 'search', 200, 1)").bind(open),
    stores.turso.prepare("INSERT INTO metrics (hour, searches, total_ms) VALUES (?1, 1, 9.5)").bind(open),
    stores.turso.prepare("INSERT INTO actor_deltas (bucket, actors_delta) VALUES (?1, 4)").bind(open),
    stores.turso.prepare("UPDATE counters SET value = value + 4 WHERE name = 'actors_total'"),
  ]);
  const { run } = await reports();
  expect(run.differences).toBe(0);
  expect(run.open_differences).toBeGreaterThan(5);
  expect(run.sections.payload_stats!.open_differences).toBeGreaterThan(0);
  expect(run.sections.payload_requests!.open_differences).toBeGreaterThan(0);
});

test("a store that has rolled old buckets into its daily table is not reported as missing them", async () => {
  await seed(stores.turso, stores.mirror, { agree: true });
  await runBackfill(backfill, { cut: CUT, dry_run: false, limit: 500 });
  setSystemTime(new Date(NOW + 5 * 86_400_000));
  await materializeStats(stores.turso, { KV: { async put() {} } });
  const { run, report } = await reports();
  expect(run.differences).toBe(0);
  const first = report("traffic_buckets", "rows")[0]!;
  expect(first.from).toBe((await stores.turso.prepare("SELECT min(bucket) AS b FROM traffic_buckets").first<{ b: number }>())!.b);
  expect(first.from).toBeGreaterThan((await stores.mirror.prepare("SELECT min(bucket) AS b FROM traffic_buckets").first<{ b: number }>())!.b);
});

test("an open bound is refused, and the endpoint needs the admin secret", async () => {
  const request = { section: "metrics", cursor: null, from: 0, limit: 100 } as const;
  await expect(compare({ ...request, closed_before: CLOSED + 1 })).rejects.toBeInstanceOf(ParityInputError);

  const fields: Partial<Env> = { ADMIN_SECRET: "s3cret", STATS_DB: stores.d1, RATE_LIMITER_STRICT: { limit: async () => ({ success: true }) } };
  // SAFETY: the handler reads ADMIN_SECRET, STATS_DB and, on a refused
  // request, the strict rate limiter.
  const env = fields as Env;
  const post = (body: Partial<ParityRequest>, secret = "s3cret") => handleStatsParity(new Request("https://typeahead.waow.tech/admin/stats-parity", {
    method: "POST", headers: { Authorization: `Bearer ${secret}` }, body: JSON.stringify(body),
  }), stores.turso, env);
  expect((await post({ ...request, closed_before: null }, "wrong")).status).toBe(401);
  expect((await post(request)).status).toBe(400);
  expect((await post({ ...request, closed_before: CLOSED + 1 })).status).toBe(400);
  const ok = await post({ ...request, closed_before: null });
  expect(ok.status).toBe(200);
  expect(await ok.json<ParityResult>()).toMatchObject({ section: "metrics", closed_before: CLOSED, differences: 0, more: null });
});

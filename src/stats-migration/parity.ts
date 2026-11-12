import { z } from "zod";
import type { SqlDb, SqlRow } from "../db";
import { readRequestPayload, readStatsPayload } from "../enrichment";
import { readTotals } from "./backfill";
import { readProfile } from "../stats-profile";
import {
  DAILY_NAMES, DAILY_TABLES, RANGED_NAMES, RANGED_TABLES,
  bucketTime, differences, diffRows, readPagePair, statRows,
  type DailyName, type RangedName, type RowDiff, type StatRow,
} from "./rows";

/** Comparing what Turso and D1 hold (docs/plans/stats-off-turso.md, steps 4
 *  and 5), one bounded section a call. This is the gate for reading from D1.
 *
 *  Only closed buckets are compared: a bucket still being written can differ
 *  between two stores that will agree a second later. `closed_before` is the
 *  first bucket left out; every section of one run uses the same one.
 *
 *  - the five time-keyed tables, row for row, in pages
 *  - `daily`: per-day history as the cron reads it, the daily table plus the
 *    buckets not yet rolled into it. The two stores roll up on different
 *    schedules, so the daily tables themselves are not compared.
 *  - `totals`: traffic_sources.hits and counters.actors_total as each store
 *    had them at `closed_before` (its value minus its own count since)
 *  - `payload_*`: the KV values the cron would write from each store at the
 *    same instant, series by series. Rows that include an open bucket are
 *    counted apart, as `open_differences`. */
export const PARITY_SECTIONS = [...RANGED_NAMES, "daily", "totals", "payload_stats", "payload_requests", "payload_profile"] as const;
export type ParitySection = (typeof PARITY_SECTIONS)[number];

export const parityRequestSchema = z.object({
  section: z.enum(PARITY_SECTIONS),
  /** null on a section's first call */
  cursor: z.number().int().nonnegative().nullable(),
  /** the first 5-minute bucket the time-keyed tables are compared from */
  from: z.number().int().nonnegative(),
  /** null to take the newest closed bucket */
  closed_before: z.number().int().nullable(),
  limit: z.number().int().min(1).max(5000),
});
export type ParityRequest = z.infer<typeof parityRequestSchema>;

interface SeriesDiff { closed: RowDiff; open: RowDiff }

export type ParityReport =
  | { kind: "rows"; table: RangedName; from: number; to: number; diff: RowDiff }
  | { kind: "daily"; tables: Record<DailyName, RowDiff> }
  | { kind: "totals"; traffic_sources: RowDiff; actors_total: { turso: number | null; d1: number | null } }
  | {
    kind: "payload";
    series: Record<string, SeriesDiff>;
    /** figures that are one number, not a series */
    scalars: Record<string, { turso: number; d1: number }>;
  }
  | { kind: "profile"; differing_bands: string[] };

export interface ParityResult {
  section: ParitySection;
  closed_before: number;
  closed_before_utc: string;
  /** differences in closed data: any of these blocks the read cutover */
  differences: number;
  /** differences in rows that include a bucket still open */
  open_differences: number;
  report: ParityReport;
  /** where this section's next page starts, or null when the section is done */
  more: number | null;
}

export class ParityInputError extends Error {}

async function oldestKey(db: SqlDb, table: RangedName): Promise<number> {
  const row = await db.prepare(`SELECT min(${RANGED_TABLES[table].key}) AS oldest FROM ${table}`).first<{ oldest: number | null }>();
  return row?.oldest ?? 0;
}

async function rangedSection(turso: SqlDb, d1: SqlDb, table: RangedName, request: ParityRequest, closedBefore: number) {
  const spec = RANGED_TABLES[table];
  const end = Math.floor(closedBefore / spec.bucketsPerKey);
  let start = request.cursor;
  if (start === null) {
    start = Math.floor(request.from / spec.bucketsPerKey);
    // a store that has rolled its oldest buckets into the daily table is not
    // missing them; the `daily` section compares what they add up to
    if (spec.rolledUp) start = Math.max(start, ...await Promise.all([oldestKey(turso, table), oldestKey(d1, table)]));
  }
  const page = start < end ? await readPagePair(turso, d1, table, start, end, request.limit) : { from: start, to: end, turso: [], d1: [] };
  const diff = diffRows(page.turso, page.d1, spec.primaryKey);
  return {
    differences: differences(diff), open_differences: 0,
    report: { kind: "rows", table, from: page.from, to: page.to, diff } satisfies ParityReport,
    more: page.to < end ? page.to : null,
  };
}

function dailyQuery(db: SqlDb, name: DailyName, closedBefore: number) {
  const { buckets, primaryKey } = DAILY_TABLES[name];
  const dimensions = primaryKey.slice(1).join(", ");
  return db.prepare(
    `SELECT day, ${dimensions}, SUM(hits) AS hits FROM (
       SELECT day, ${dimensions}, hits FROM ${name}
       UNION ALL
       SELECT bucket / 288 AS day, ${dimensions}, SUM(hits) AS hits
         FROM ${buckets} WHERE bucket < ?1 GROUP BY bucket / 288, ${dimensions}
     ) GROUP BY day, ${dimensions} ORDER BY day, ${dimensions}`,
  ).bind(closedBefore);
}

async function dailySection(turso: SqlDb, d1: SqlDb, closedBefore: number) {
  const read = async (db: SqlDb) => {
    const results = await db.batch(DAILY_NAMES.map((name) => dailyQuery(db, name, closedBefore)), "read");
    return DAILY_NAMES.map((name, i) => statRows(results[i]!.results, DAILY_TABLES[name].columns));
  };
  const [source, mirror] = await Promise.all([read(turso), read(d1)]);
  const diffs = DAILY_NAMES.map((name, i) => diffRows(source[i]!, mirror[i]!, DAILY_TABLES[name].primaryKey));
  return {
    differences: diffs.reduce((sum, diff) => sum + differences(diff), 0), open_differences: 0,
    report: { kind: "daily", tables: { traffic_daily: diffs[0]!, request_daily: diffs[1]! } } satisfies ParityReport,
    more: null,
  };
}

async function totalsSection(turso: SqlDb, d1: SqlDb, closedBefore: number) {
  const atClose = async (db: SqlDb) => {
    const totals = await readTotals(db, closedBefore);
    return {
      sources: totals.sources
        .map((row) => ({ domain: String(row.domain), hits: Number(row.hits) - (totals.sinceCut.get(String(row.domain)) ?? 0) }))
        .filter((row) => row.hits !== 0),
      actors: totals.actorsTotal === null ? null : totals.actorsTotal - totals.actorsSinceCut,
    };
  };
  const [source, mirror] = await Promise.all([atClose(turso), atClose(d1)]);
  const sources = diffRows(source.sources, mirror.sources, ["domain"]);
  return {
    differences: differences(sources) + (source.actors === mirror.actors ? 0 : 1), open_differences: 0,
    report: { kind: "totals", traffic_sources: sources, actors_total: { turso: source.actors, d1: mirror.actors } } satisfies ParityReport,
    more: null,
  };
}

/** One series of a KV value from each store. Rows whose time key is at or
 *  past `openFrom` include an open bucket. A series the cron reads with a row
 *  limit starts wherever each store's newest rows put it, so `limited` drops
 *  the rows before the later of the two starts. */
function seriesDiff(a: SqlRow[], b: SqlRow[], primaryKey: readonly string[], values: readonly string[], openFrom: number, limited = false): SeriesDiff {
  const time = primaryKey[0]!;
  // libSQL rows carry their values by position as well as by name; keep the named columns
  const turso = statRows(a, [...primaryKey, ...values]);
  const d1 = statRows(b, [...primaryKey, ...values]);
  const starts = [turso, d1].filter((rows) => rows.length > 0).map((rows) => Math.min(...rows.map((row) => Number(row[time]))));
  const from = limited && starts.length === 2 ? Math.max(...starts) : 0;
  const part = (rows: StatRow[], open: boolean) => rows.filter((row) => Number(row[time]) >= from && (Number(row[time]) >= openFrom) === open);
  return {
    closed: diffRows(part(turso, false), part(d1, false), primaryKey),
    open: diffRows(part(turso, true), part(d1, true), primaryKey),
  };
}

function payloadResult(series: Record<string, SeriesDiff>, scalars: Record<string, { turso: number; d1: number }>) {
  const all = Object.values(series);
  return {
    differences: all.reduce((sum, diff) => sum + differences(diff.closed), 0)
      + Object.values(scalars).filter((value) => value.turso !== value.d1).length,
    open_differences: all.reduce((sum, diff) => sum + differences(diff.open), 0),
    report: { kind: "payload", series, scalars } satisfies ParityReport,
    more: null,
  };
}

async function statsPayloadSection(turso: SqlDb, d1: SqlDb, closedBefore: number, now: number) {
  const [a, b] = await Promise.all([readStatsPayload(turso, now), readStatsPayload(d1, now)]);
  const hour = Math.floor(closedBefore / 12);
  const closedSearches = (rows: typeof a.metrics) =>
    rows.filter((row) => row.hour < closedBefore).reduce((sum, row) => sum + row.searches + row.cache_hits, 0);
  const lastClosed = (rows: typeof a.snapshots) => rows.filter((row) => row.hour < hour).at(-1);
  return payloadResult({
    metrics: seriesDiff(a.metrics, b.metrics, ["hour"], ["searches", "total_ms", "cache_hits", "cache_ms"], closedBefore, true),
    snapshots: seriesDiff(a.snapshots, b.snapshots, ["hour"], ["total", "with_handles", "with_avatars", "hidden"], hour, true),
    deltas: seriesDiff(a.deltas, b.deltas, ["bucket"], ["actors_delta", "handles_delta", "avatars_delta"], closedBefore, true),
    traffic_series: seriesDiff(a.traffic_series ?? [], b.traffic_series ?? [], ["hour", "domain"], ["hits"], hour),
    traffic_series_5m: seriesDiff(a.traffic_series_5m ?? [], b.traffic_series_5m ?? [], ["bucket", "domain"], ["hits"], closedBefore),
    traffic_daily: seriesDiff(a.traffic_daily ?? [], b.traffic_daily ?? [], ["day", "domain"], ["hits"], Math.floor(closedBefore / 288)),
  }, {
    // the four figures on the default link-preview card, as of closed_before
    card_apps: { turso: a.traffic_sources_total ?? 0, d1: b.traffic_sources_total ?? 0 },
    card_searches: { turso: closedSearches(a.metrics), d1: closedSearches(b.metrics) },
    card_actors: { turso: lastClosed(a.snapshots)?.total ?? 0, d1: lastClosed(b.snapshots)?.total ?? 0 },
    card_with_avatars: { turso: lastClosed(a.snapshots)?.with_avatars ?? 0, d1: lastClosed(b.snapshots)?.with_avatars ?? 0 },
  });
}

async function requestPayloadSection(turso: SqlDb, d1: SqlDb, closedBefore: number, now: number) {
  const [a, b] = await Promise.all([readRequestPayload(turso, now), readRequestPayload(d1, now)]);
  const dimensions = ["domain", "endpoint", "status"];
  return payloadResult({
    series: seriesDiff(a.series ?? [], b.series ?? [], ["hour", ...dimensions], ["hits"], Math.floor(closedBefore / 12)),
    series_5m: seriesDiff(a.series_5m ?? [], b.series_5m ?? [], ["bucket", ...dimensions], ["hits"], closedBefore),
    daily: seriesDiff(a.daily ?? [], b.daily ?? [], ["day", ...dimensions], ["hits"], Math.floor(closedBefore / 288)),
  }, {});
}

/** The profile is built from buckets that ended at least a day ago, so every
 *  band is closed. */
async function profileSection(turso: SqlDb, d1: SqlDb, now: number) {
  const [a, b] = await Promise.all([readProfile(turso, now), readProfile(d1, now)]);
  const differing: string[] = [];
  for (const kind of ["source", "requests"] as const) {
    for (const grain of ["m5", "h"] as const) {
      a[kind][grain].forEach((band, hour) => {
        if (JSON.stringify(band) !== JSON.stringify(b[kind][grain][hour])) differing.push(`${kind}.${grain}[${hour}]`);
      });
    }
  }
  return {
    differences: differing.length, open_differences: 0,
    report: { kind: "profile", differing_bands: differing } satisfies ParityReport,
    more: null,
  };
}

export async function parityStep(turso: SqlDb, d1: SqlDb, request: ParityRequest, now = Date.now()): Promise<ParityResult> {
  const newestClosed = Math.floor(now / 300_000) - 1;
  const closedBefore = request.closed_before ?? newestClosed;
  if (closedBefore > newestClosed) throw new ParityInputError(`closed_before ${closedBefore} is still open: the newest closed bound is ${newestClosed}`);
  const { section } = request;
  const done = section === "daily" ? await dailySection(turso, d1, closedBefore)
    : section === "totals" ? await totalsSection(turso, d1, closedBefore)
    : section === "payload_stats" ? await statsPayloadSection(turso, d1, closedBefore, now)
    : section === "payload_requests" ? await requestPayloadSection(turso, d1, closedBefore, now)
    : section === "payload_profile" ? await profileSection(turso, d1, now)
    : await rangedSection(turso, d1, section, request, closedBefore);
  return {
    section, closed_before: closedBefore, closed_before_utc: bucketTime(closedBefore),
    ...done,
  };
}

export interface ParityRun {
  closed_before: number | null;
  calls: number;
  differences: number;
  open_differences: number;
  sections: Partial<Record<ParitySection, { calls: number; compared: number; differences: number; open_differences: number }>>;
}

function compared(report: ParityReport): number {
  const rows = (diff: RowDiff) => Math.max(diff.turso_rows, diff.d1_rows);
  switch (report.kind) {
    case "rows": return rows(report.diff);
    case "daily": return rows(report.tables.traffic_daily) + rows(report.tables.request_daily);
    case "totals": return rows(report.traffic_sources) + 1;
    case "payload": return Object.values(report.series).reduce((sum, diff) => sum + rows(diff.closed), 0) + Object.keys(report.scalars).length;
    case "profile": return 96;
  }
}

/** Every listed section in order, all against one closed bound. `call` is the
 *  admin endpoint for an operator and `parityStep` in tests. Before a
 *  backfill only the time-keyed tables from the start of dual write can
 *  agree, so that run lists those sections and sets `from`. */
export async function runParity(
  call: (request: ParityRequest) => Promise<ParityResult>,
  options: { from: number; limit: number; sections: readonly ParitySection[] },
  onResult: (result: ParityResult) => void = () => {},
): Promise<ParityRun> {
  const run: ParityRun = { closed_before: null, calls: 0, differences: 0, open_differences: 0, sections: {} };
  for (const name of options.sections) {
    let cursor: number | null = null;
    do {
      const result: ParityResult = await call({ section: name, cursor, from: options.from, closed_before: run.closed_before, limit: options.limit });
      onResult(result);
      cursor = result.more;
      run.closed_before = result.closed_before;
      run.calls++;
      run.differences += result.differences;
      run.open_differences += result.open_differences;
      const section = run.sections[result.section] ?? { calls: 0, compared: 0, differences: 0, open_differences: 0 };
      section.calls++;
      section.compared += compared(result.report);
      section.differences += result.differences;
      section.open_differences += result.open_differences;
      run.sections[result.section] = section;
    } while (cursor !== null);
  }
  return run;
}

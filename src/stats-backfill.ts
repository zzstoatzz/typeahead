import { z } from "zod";
import type { SqlDb, SqlRow } from "./db";
import {
  DAILY_NAMES, DAILY_TABLES, DUAL_WRITE_START_BUCKET, RANGED_NAMES, RANGED_TABLES,
  bucketTime, differences, diffRows, insertStatements, readPagePair, statRows,
  type DailyName, type RangedName, type RowDiff, type StatRow,
} from "./stats-rows";

/** Copying the statistics history from Turso into D1 (docs/plans/stats-off-turso.md,
 *  step 3), one bounded step a call.
 *
 *  Everything is relative to `cut`, a closed 5-minute bucket at or after the
 *  start of dual write. Before the cut D1 is made a copy of Turso: each page
 *  replaces D1's rows for a key range with Turso's, so the buckets D1 counted
 *  by itself between the start of dual write and the cut are replaced too and
 *  nothing is counted twice. From the cut on D1 keeps its own rows. The two
 *  running totals are set to Turso's value as it stood at the cut plus what D1
 *  counted since:
 *
 *    traffic_sources.hits = turso.hits - sum(turso.traffic_buckets >= cut)
 *                                      + sum(d1.traffic_buckets >= cut)
 *    counters.actors_total = turso.value - sum(turso.actor_deltas >= cut)
 *                                        + sum(d1.actor_deltas >= cut)
 *
 *  Each step reads what both stores hold, writes only when they differ, and
 *  takes its position from the request, so a run can stop anywhere and start
 *  again from any step, and a second run over an unchanged Turso writes
 *  nothing. `counters.label_stream_seq` is the ingester's label cursor, not a
 *  statistic, and is never copied. */
export const BACKFILL_STEPS = [...RANGED_NAMES, "daily", "totals"] as const;
export type BackfillStep = (typeof BACKFILL_STEPS)[number];

export const backfillRequestSchema = z.object({
  step: z.enum(BACKFILL_STEPS),
  cursor: z.number().int().nonnegative(),
  cut: z.number().int(),
  limit: z.number().int().min(1).max(5000),
  dry_run: z.boolean(),
});
export type BackfillRequest = z.infer<typeof backfillRequestSchema>;

export type BackfillReport =
  | { kind: "rows"; table: RangedName; from: number; to: number; diff: RowDiff }
  | { kind: "daily"; tables: Record<DailyName, RowDiff>; rolled_up_in_turso: Record<DailyName, number> }
  | {
    kind: "totals";
    /** `turso` in this diff is the value D1 should hold, not Turso's current one */
    traffic_sources: RowDiff;
    actors_total: { turso: number; turso_since_cut: number; d1_since_cut: number; target: number; d1: number | null };
  };

export interface BackfillPosition { step: BackfillStep; cursor: number }

export interface BackfillResult {
  step: BackfillStep;
  cut: number;
  cut_utc: string;
  dry_run: boolean;
  /** rows D1 holds differently from what this step makes it hold */
  changes: number;
  wrote: boolean;
  report: BackfillReport;
  next: BackfillPosition | null;
}

export class BackfillInputError extends Error {}

/** The largest daily history one step replaces in a single D1 transaction.
 *  Turso held 659 traffic_daily rows and no request_daily rows on 2026-11-12. */
const DAILY_ROWS_MAX = 12_000;

function checkCut(cut: number, now: number): void {
  const current = Math.floor(now / 300_000);
  if (cut < DUAL_WRITE_START_BUCKET) {
    throw new BackfillInputError(`cut ${cut} is before dual write started (${DUAL_WRITE_START_BUCKET}): D1 has no count of its own from there`);
  }
  if (cut > current - 1) {
    throw new BackfillInputError(`cut ${cut} is not closed yet: it must be at most ${current - 1}, so writes to the bucket before it have landed`);
  }
}

function following(step: BackfillStep): BackfillResult["next"] {
  const next = BACKFILL_STEPS[BACKFILL_STEPS.indexOf(step) + 1];
  return next === undefined ? null : { step: next, cursor: 0 };
}

async function rangedStep(turso: SqlDb, d1: SqlDb, table: RangedName, request: BackfillRequest) {
  const spec = RANGED_TABLES[table];
  const end = Math.floor(request.cut / spec.bucketsPerKey);
  const page = await readPagePair(turso, d1, table, request.cursor, end, request.limit);
  const diff = diffRows(page.turso, page.d1, spec.primaryKey);
  const changes = differences(diff);
  const wrote = !request.dry_run && changes > 0;
  if (wrote) {
    await d1.batch([
      d1.prepare(`DELETE FROM ${table} WHERE ${spec.key} >= ?1 AND ${spec.key} < ?2`).bind(page.from, page.to),
      ...insertStatements(d1, table, spec.columns, page.turso),
    ]);
  }
  return {
    changes, wrote,
    report: { kind: "rows", table, from: page.from, to: page.to, diff } satisfies BackfillReport,
    next: page.to < end ? { step: table, cursor: page.to } : following(table),
  };
}

const firstNumber = (rows: SqlRow[], column: string): number | null => {
  const value = rows[0]?.[column];
  return value === null || value === undefined ? null : Number(value);
};

/** The two daily tables, whole, in one D1 transaction, together with the
 *  removal of the D1 buckets Turso has already rolled into them. The cron
 *  moves buckets older than 90 days into the daily table every hour, so the
 *  daily rows and the oldest bucket are read in one Turso transaction, and
 *  this step runs after the bucket pages: whatever a rollup moved while they
 *  were being copied is in the daily rows read here and leaves D1's buckets. */
async function dailyStep(turso: SqlDb, d1: SqlDb, request: BackfillRequest) {
  const source = await turso.batch(DAILY_NAMES.flatMap((name) => [
    turso.prepare(`SELECT ${DAILY_TABLES[name].columns.join(", ")} FROM ${name} ORDER BY ${DAILY_TABLES[name].primaryKey.join(", ")}`),
    turso.prepare(`SELECT min(bucket) AS oldest FROM ${DAILY_TABLES[name].buckets}`),
  ]), "read");
  const oldest = DAILY_NAMES.map((_, i) => firstNumber(source[i * 2 + 1]!.results, "oldest") ?? 0);
  const mirror = await d1.batch(DAILY_NAMES.flatMap((name, i) => [
    d1.prepare(`SELECT ${DAILY_TABLES[name].columns.join(", ")} FROM ${name} ORDER BY ${DAILY_TABLES[name].primaryKey.join(", ")}`),
    d1.prepare(`SELECT count(*) AS n FROM ${DAILY_TABLES[name].buckets} WHERE bucket < ?1`).bind(oldest[i]!),
  ]), "read");

  const rows = DAILY_NAMES.map((name, i) => statRows(source[i * 2]!.results, DAILY_TABLES[name].columns));
  if (rows.some((table) => table.length > DAILY_ROWS_MAX)) {
    throw new Error(`a daily table holds more than ${DAILY_ROWS_MAX} rows, too many for one D1 transaction`);
  }
  const diffs = DAILY_NAMES.map((name, i) =>
    diffRows(rows[i]!, statRows(mirror[i * 2]!.results, DAILY_TABLES[name].columns), DAILY_TABLES[name].primaryKey));
  const rolledUp = DAILY_NAMES.map((_, i) => firstNumber(mirror[i * 2 + 1]!.results, "n") ?? 0);
  const changes = diffs.reduce((sum, diff) => sum + differences(diff), 0) + rolledUp.reduce((sum, n) => sum + n, 0);
  const wrote = !request.dry_run && changes > 0;
  if (wrote) {
    await d1.batch(DAILY_NAMES.flatMap((name, i) => [
      d1.prepare(`DELETE FROM ${name}`),
      ...insertStatements(d1, name, DAILY_TABLES[name].columns, rows[i]!),
      d1.prepare(`DELETE FROM ${DAILY_TABLES[name].buckets} WHERE bucket < ?1`).bind(oldest[i]!),
    ]));
  }
  return {
    changes, wrote,
    report: {
      kind: "daily",
      tables: { traffic_daily: diffs[0]!, request_daily: diffs[1]! },
      rolled_up_in_turso: { traffic_daily: rolledUp[0]!, request_daily: rolledUp[1]! },
    } satisfies BackfillReport,
    next: following("daily"),
  };
}

function totalsQueries(db: SqlDb, cut: number) {
  return [
    db.prepare("SELECT domain, hits FROM traffic_sources ORDER BY domain"),
    db.prepare("SELECT domain, SUM(hits) AS hits FROM traffic_buckets WHERE bucket >= ?1 GROUP BY domain").bind(cut),
    db.prepare("SELECT value FROM counters WHERE name = 'actors_total'"),
    db.prepare("SELECT coalesce(SUM(actors_delta), 0) AS n FROM actor_deltas WHERE bucket >= ?1").bind(cut),
  ];
}

export async function readTotals(db: SqlDb, cut: number) {
  const [sources, since, counter, deltas] = await db.batch(totalsQueries(db, cut), "read");
  const sinceCut = new Map(statRows(since!.results, ["domain", "hits"]).map((row) => [String(row.domain), Number(row.hits)]));
  return {
    sources: statRows(sources!.results, ["domain", "hits"]),
    sinceCut,
    actorsTotal: firstNumber(counter!.results, "value"),
    actorsSinceCut: firstNumber(deltas!.results, "n") ?? 0,
  };
}

/** The running totals. Each store's four reads are one transaction, and each
 *  store writes a hit and its bucket in one transaction, so `hits - since the
 *  cut` is that store's total at the cut whenever it is read. D1's own part is
 *  added inside the D1 transaction that writes the result, so a hit landing
 *  during the step is neither lost nor counted twice. */
async function totalsStep(turso: SqlDb, d1: SqlDb, request: BackfillRequest) {
  const { cut } = request;
  const [source, mirror] = await Promise.all([readTotals(turso, cut), readTotals(d1, cut)]);
  if (source.actorsTotal === null) throw new Error("Turso has no counters.actors_total row");

  const atCut = source.sources.map((row) => {
    const hits = Number(row.hits) - (source.sinceCut.get(String(row.domain)) ?? 0);
    if (hits < 0) throw new Error(`traffic_sources ${row.domain}: Turso holds fewer hits than its buckets since the cut`);
    return { domain: String(row.domain), hits };
  });
  const inTurso = new Set(atCut.map((row) => row.domain));
  const target: StatRow[] = [
    ...atCut.map((row) => ({ domain: row.domain, hits: row.hits + (mirror.sinceCut.get(row.domain) ?? 0) })),
    ...[...mirror.sinceCut].filter(([domain]) => !inTurso.has(domain)).map(([domain, hits]) => ({ domain, hits })),
  ].sort((a, b) => (a.domain < b.domain ? -1 : 1));
  const sources = diffRows(target, mirror.sources, ["domain"]);

  const actorsAtCut = source.actorsTotal - source.actorsSinceCut;
  const actorsTarget = actorsAtCut + mirror.actorsSinceCut;
  const changes = differences(sources) + (mirror.actorsTotal === actorsTarget ? 0 : 1);
  const wrote = !request.dry_run && changes > 0;
  if (wrote) {
    const perStatement = 49;
    const inserts = [];
    for (let i = 0; i < atCut.length; i += perStatement) {
      const chunk = atCut.slice(i, i + perStatement);
      inserts.push(d1.prepare(
        `INSERT INTO traffic_sources (domain, hits)
         SELECT at_cut.column1, at_cut.column2 + coalesce(
           (SELECT SUM(hits) FROM traffic_buckets WHERE domain = at_cut.column1 AND bucket >= ?1), 0)
         FROM (VALUES ${chunk.map((_, r) => `(?${r * 2 + 2}, ?${r * 2 + 3})`).join(", ")}) AS at_cut`,
      ).bind(cut, ...chunk.flatMap((row) => [row.domain, row.hits])));
    }
    await d1.batch([
      d1.prepare("DELETE FROM traffic_sources"),
      ...inserts,
      d1.prepare(
        `INSERT OR IGNORE INTO traffic_sources (domain, hits)
         SELECT domain, SUM(hits) FROM traffic_buckets WHERE bucket >= ?1 GROUP BY domain`,
      ).bind(cut),
      d1.prepare(
        `INSERT INTO counters (name, value)
         VALUES ('actors_total', ?1 + (SELECT coalesce(SUM(actors_delta), 0) FROM actor_deltas WHERE bucket >= ?2))
         ON CONFLICT(name) DO UPDATE SET value = excluded.value`,
      ).bind(actorsAtCut, cut),
    ]);
  }
  return {
    changes, wrote,
    report: {
      kind: "totals",
      traffic_sources: sources,
      actors_total: {
        turso: source.actorsTotal, turso_since_cut: source.actorsSinceCut,
        d1_since_cut: mirror.actorsSinceCut, target: actorsTarget, d1: mirror.actorsTotal,
      },
    } satisfies BackfillReport,
    next: following("totals"),
  };
}

export async function backfillStep(turso: SqlDb, d1: SqlDb, request: BackfillRequest, now = Date.now()): Promise<BackfillResult> {
  checkCut(request.cut, now);
  const { step } = request;
  const done = step === "daily" ? await dailyStep(turso, d1, request)
    : step === "totals" ? await totalsStep(turso, d1, request)
    : await rangedStep(turso, d1, step, request);
  return { step, cut: request.cut, cut_utc: bucketTime(request.cut), dry_run: request.dry_run, ...done };
}

export interface BackfillRun {
  cut: number;
  dry_run: boolean;
  calls: number;
  changes: number;
  steps: Record<BackfillStep, { calls: number; changes: number; wrote: number }>;
}

/** Every step in order from `start`, each call one bounded piece of work.
 *  `call` is the admin endpoint for an operator and `backfillStep` in tests. */
export async function runBackfill(
  call: (request: BackfillRequest) => Promise<BackfillResult>,
  options: { cut: number; dry_run: boolean; limit: number; start?: BackfillPosition },
  onResult: (result: BackfillResult) => void = () => {},
): Promise<BackfillRun> {
  const none = () => ({ calls: 0, changes: 0, wrote: 0 });
  const run: BackfillRun = {
    cut: options.cut, dry_run: options.dry_run, calls: 0, changes: 0,
    steps: {
      metrics: none(), traffic_buckets: none(), request_buckets: none(), actor_deltas: none(), snapshots: none(),
      daily: none(), totals: none(),
    },
  };
  let position: BackfillResult["next"] = options.start ?? { step: BACKFILL_STEPS[0], cursor: 0 };
  while (position !== null) {
    const result = await call({ ...position, cut: options.cut, limit: options.limit, dry_run: options.dry_run });
    onResult(result);
    run.calls++;
    run.changes += result.changes;
    const step = run.steps[result.step];
    step.calls++;
    step.changes += result.changes;
    if (result.wrote) step.wrote++;
    position = result.next;
  }
  return run;
}

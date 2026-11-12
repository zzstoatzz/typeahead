import { z } from "zod";
import type { SqlDb, SqlRow, Stmt } from "../db";

/** `T0` of docs/plans/stats-off-turso.md: the first 5-minute bucket that began
 *  after dual write was live everywhere, 2026-11-10 19:20:00 UTC. D1 holds its
 *  own count of every bucket from here on and nothing reliable before it. */
export const DUAL_WRITE_START_BUCKET = 5_981_128;

/** A statistics table whose rows are filed under a time key. `bucketsPerKey`
 *  converts a 5-minute bucket number to the key's unit; `rolledUp` marks the
 *  two tables the cron prunes into a daily table after 90 days. */
interface RangedTable {
  key: string;
  columns: readonly string[];
  primaryKey: readonly string[];
  bucketsPerKey: number;
  rolledUp: boolean;
}

export const RANGED_TABLES = {
  metrics: { key: "hour", columns: ["hour", "searches", "total_ms", "cache_hits", "cache_ms"], primaryKey: ["hour"], bucketsPerKey: 1, rolledUp: false },
  traffic_buckets: { key: "bucket", columns: ["bucket", "domain", "hits"], primaryKey: ["bucket", "domain"], bucketsPerKey: 1, rolledUp: true },
  request_buckets: { key: "bucket", columns: ["bucket", "domain", "endpoint", "status", "hits"], primaryKey: ["bucket", "domain", "endpoint", "status"], bucketsPerKey: 1, rolledUp: true },
  actor_deltas: { key: "bucket", columns: ["bucket", "actors_delta", "handles_delta", "avatars_delta"], primaryKey: ["bucket"], bucketsPerKey: 1, rolledUp: false },
  snapshots: { key: "hour", columns: ["hour", "total", "with_handles", "with_avatars", "hidden"], primaryKey: ["hour"], bucketsPerKey: 12, rolledUp: false },
} as const satisfies Record<string, RangedTable>;

export type RangedName = keyof typeof RANGED_TABLES;
export const RANGED_NAMES = ["metrics", "traffic_buckets", "request_buckets", "actor_deltas", "snapshots"] as const satisfies readonly RangedName[];

export const DAILY_TABLES = {
  traffic_daily: { columns: ["day", "domain", "hits"], primaryKey: ["day", "domain"], buckets: "traffic_buckets" },
  request_daily: { columns: ["day", "domain", "endpoint", "status", "hits"], primaryKey: ["day", "domain", "endpoint", "status"], buckets: "request_buckets" },
} as const;

export type DailyName = keyof typeof DAILY_TABLES;
export const DAILY_NAMES = ["traffic_daily", "request_daily"] as const satisfies readonly DailyName[];

/** one statistics row: every column of every statistics table is an integer, a real or text */
export type StatRow = Record<string, string | number>;

const cell = z.union([z.string(), z.number()]);

/** the store boundary: a null, blob or bigint in a statistics column is refused */
export function statRows(rows: SqlRow[], columns: readonly string[]): StatRow[] {
  return rows.map((row) => {
    const out: StatRow = {};
    for (const column of columns) {
      const value = cell.safeParse(row[column]);
      if (!value.success) throw new Error(`statistics column ${column} does not hold a number or text`);
      out[column] = value.data;
    }
    return out;
  });
}

export function bucketTime(bucket: number): string {
  return new Date(bucket * 300_000).toISOString();
}

async function readRange(db: SqlDb, name: RangedName, from: number, to: number, limit: number): Promise<StatRow[]> {
  const spec = RANGED_TABLES[name];
  const { results } = await db.prepare(
    `SELECT ${spec.columns.join(", ")} FROM ${name} WHERE ${spec.key} >= ?1 AND ${spec.key} < ?2 ORDER BY ${spec.primaryKey.join(", ")} LIMIT ?3`,
  ).bind(from, to, limit).all<SqlRow>();
  return statRows(results, spec.columns);
}

export interface PagePair {
  from: number;
  /** exclusive; equal to `end` on a table's last page */
  to: number;
  turso: StatRow[];
  d1: StatRow[];
}

/** Both stores' rows for the keys in [from, to), where `to` is chosen so that
 *  neither side returns more than `limit` rows and no key is split between
 *  two pages. Keys are integers, so the page always advances. */
export async function readPagePair(turso: SqlDb, d1: SqlDb, name: RangedName, from: number, end: number, limit: number): Promise<PagePair> {
  const { key } = RANGED_TABLES[name];
  const [a, b] = await Promise.all([readRange(turso, name, from, end, limit + 1), readRange(d1, name, from, end, limit + 1)]);
  const overflow = [a, b].filter((rows) => rows.length > limit).map((rows) => Number(rows[limit]![key]));
  const to = overflow.length > 0 ? Math.min(...overflow) : end;
  if (to <= from && from < end) throw new Error(`${name}: key ${from} alone holds more than ${limit} rows; raise the limit`);
  const within = (rows: StatRow[]) => rows.filter((row) => Number(row[key]) < to);
  return { from, to, turso: within(a), d1: within(b) };
}

export interface RowDiff {
  turso_rows: number;
  d1_rows: number;
  same: number;
  differing: number;
  only_turso: number;
  only_d1: number;
  /** the first few rows that are not the same in both stores */
  examples: { turso: StatRow | null; d1: StatRow | null }[];
}

const EXAMPLES = 20;

export function differences(diff: RowDiff): number {
  return diff.differing + diff.only_turso + diff.only_d1;
}

/** Row-for-row comparison on the primary key; every other column must be
 *  identical, reals included. */
export function diffRows(turso: StatRow[], d1: StatRow[], primaryKey: readonly string[]): RowDiff {
  const keyOf = (row: StatRow) => JSON.stringify(primaryKey.map((column) => row[column]));
  const mirror = new Map(d1.map((row) => [keyOf(row), row]));
  const diff: RowDiff = { turso_rows: turso.length, d1_rows: d1.length, same: 0, differing: 0, only_turso: 0, only_d1: 0, examples: [] };
  const note = (a: StatRow | null, b: StatRow | null) => { if (diff.examples.length < EXAMPLES) diff.examples.push({ turso: a, d1: b }); };
  for (const row of turso) {
    const key = keyOf(row);
    const other = mirror.get(key);
    mirror.delete(key);
    if (other === undefined) { diff.only_turso++; note(row, null); }
    else if (Object.keys(row).every((column) => row[column] === other[column])) diff.same++;
    else { diff.differing++; note(row, other); }
  }
  for (const row of mirror.values()) { diff.only_d1++; note(null, row); }
  return diff;
}

/** D1 allows 100 bound parameters a statement, so rows go in as multi-row
 *  VALUES lists of that size. Values are bound, never written into the SQL or
 *  passed through JSON, so a real arrives as the double it left as. */
const MAX_PARAMETERS = 100;

export function insertStatements(db: SqlDb, table: string, columns: readonly string[], rows: StatRow[]): Stmt[] {
  const perStatement = Math.floor(MAX_PARAMETERS / columns.length);
  const statements: Stmt[] = [];
  for (let i = 0; i < rows.length; i += perStatement) {
    const chunk = rows.slice(i, i + perStatement);
    const tuples = chunk.map((_, r) => `(${columns.map((_, c) => `?${r * columns.length + c + 1}`).join(", ")})`);
    statements.push(db.prepare(`INSERT INTO ${table} (${columns.join(", ")}) VALUES ${tuples.join(", ")}`)
      .bind(...chunk.flatMap((row) => columns.map((column) => row[column]!))));
  }
  return statements;
}

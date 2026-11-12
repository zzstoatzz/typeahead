import type { SqlDb } from "./db";
import { DAILY_TABLES, DUAL_WRITE_START_BUCKET, RANGED_NAMES, RANGED_TABLES, insertStatements, type RangedName, type StatRow } from "./stats-rows";

/** Test fixture: a Turso with months of statistics history and a D1 that has
 *  only counted since dual write started, both still counting past the cut. */
export const T0 = DUAL_WRITE_START_BUCKET;
export const CUT = T0 + 300;
export const NOW = (CUT + 5) * 300_000 + 1_000;
const FIRST = T0 - 89 * 288;
const LABEL_CURSOR = { name: "label_stream_seq", value: 45_886_594 };

interface Tables {
  metrics: StatRow[];
  traffic_buckets: StatRow[];
  request_buckets: StatRow[];
  actor_deltas: StatRow[];
  snapshots: StatRow[];
}

function bucketsOf(from: number, to: number): number[] {
  const buckets = new Set([T0 - 1, T0, CUT - 1, CUT, CUT + 1]);
  for (let b = FIRST; b <= to; b += 97) buckets.add(b);
  return [...buckets].filter((b) => b >= from && b <= to).sort((a, b) => a - b);
}

function rowsAt(b: number): Tables {
  const domain = b % 2 === 0 ? "plyr.fm" : "leaflet.pub";
  return {
    metrics: [{ hour: b, searches: (b % 7) + 1, total_ms: ((b % 7) + 1) * 12.1 + 0.1, cache_hits: b % 2, cache_ms: (b % 2) * 0.7 }],
    traffic_buckets: [{ bucket: b, domain, hits: 1 + (b % 5) }, { bucket: b, domain: "unknown", hits: 2 }],
    request_buckets: [
      { bucket: b, domain, endpoint: "search", status: 200, hits: 3 + (b % 4) },
      ...(b % 3 === 0 ? [{ bucket: b, domain, endpoint: "search", status: 503, hits: 1 }] : []),
    ],
    actor_deltas: [{ bucket: b, actors_delta: (b % 11) - 2, handles_delta: 0, avatars_delta: 0 }],
    snapshots: [],
  };
}

function tables(buckets: number[], firstHour: number): Tables {
  const out: Tables = { metrics: [], traffic_buckets: [], request_buckets: [], actor_deltas: [], snapshots: [] };
  for (const b of buckets) {
    const rows = rowsAt(b);
    for (const name of RANGED_NAMES) out[name].push(...rows[name]);
  }
  for (let hour = firstHour; hour <= Math.floor(NOW / 3_600_000); hour++) {
    out.snapshots.push({ hour, total: 13_000_000 + (hour % 50), with_handles: 12_000_000, with_avatars: 9_000_000, hidden: 40 + (hour % 3) });
  }
  return out;
}

const sum = (rows: StatRow[], column: string) => rows.reduce((total, row) => total + Number(row[column]), 0);

function sources(trafficRows: StatRow[], extra: Record<string, number> = {}): StatRow[] {
  const hits = new Map(Object.entries(extra));
  for (const row of trafficRows) hits.set(String(row.domain), (hits.get(String(row.domain)) ?? 0) + Number(row.hits));
  return [...hits].map(([domain, n]) => ({ domain, hits: n })).sort((a, b) => (a.domain < b.domain ? -1 : 1));
}

async function load(db: SqlDb, name: string, columns: readonly string[], rows: StatRow[]) {
  const statements = insertStatements(db, name, columns, rows);
  for (let i = 0; i < statements.length; i += 40) await db.batch(statements.slice(i, i + 40));
}

export interface Seeded {
  turso: Tables & { traffic_daily: StatRow[]; request_daily: StatRow[]; traffic_sources: StatRow[]; actors_total: number };
  d1: Tables & { traffic_sources: StatRow[]; actors_total: number };
}

/** `agree: false` gives D1 the differences two independent stores can have:
 *  a partial bucket before T0, a dropped write and an extra row between T0
 *  and the cut, and its own counts from the cut on. */
export async function seed(turso: SqlDb, d1: SqlDb, { agree }: { agree: boolean }): Promise<Seeded> {
  const firstDay = Math.floor(FIRST / 288);
  const trafficDaily: StatRow[] = [];
  const requestDaily: StatRow[] = [];
  for (let day = firstDay - 30; day < firstDay; day++) {
    if (day % 6 === 0) trafficDaily.push({ day, domain: "old-app.example", hits: 5 });
    trafficDaily.push({ day, domain: "plyr.fm", hits: 100 + (day % 9) });
    requestDaily.push({ day, domain: "plyr.fm", endpoint: "search", status: 200, hits: 300 + (day % 4) });
  }

  const source = tables(bucketsOf(FIRST, CUT + 4), Math.floor(T0 / 12) - 200);
  const mirror = tables(bucketsOf(T0 - 1, CUT + 4), Math.floor(T0 / 12));
  const at = (name: RangedName, key: number) => mirror[name].find((row) => Number(row[RANGED_TABLES[name].key]) === key)!;
  at("traffic_buckets", T0 - 1).hits = 1;
  at("request_buckets", T0 - 1).hits = 1;
  if (!agree) {
    at("traffic_buckets", CUT - 1).hits = Number(at("traffic_buckets", CUT - 1).hits) - 1;
    at("request_buckets", CUT - 1).hits = Number(at("request_buckets", CUT - 1).hits) - 1;
    at("metrics", CUT - 1).total_ms = 3.3;
    at("actor_deltas", T0).actors_delta = 500;
    at("snapshots", Math.floor(CUT / 12) - 2).total = 1;
    mirror.traffic_buckets.push({ bucket: T0 + 1, domain: "d1-only-before-cut.example", hits: 9 });
    mirror.request_buckets.push({ bucket: T0 + 1, domain: "d1-only-before-cut.example", endpoint: "search", status: 200, hits: 9 });

    at("traffic_buckets", CUT).hits = Number(at("traffic_buckets", CUT).hits) + 2;
    at("request_buckets", CUT).hits = Number(at("request_buckets", CUT).hits) + 2;
    at("actor_deltas", CUT + 1).actors_delta = 77;
    at("snapshots", Math.floor(NOW / 3_600_000)).total = 13_000_999;
    mirror.traffic_buckets.push({ bucket: CUT + 1, domain: "d1-only-since-cut.example", hits: 4 });
  }

  const tursoSources = sources(source.traffic_buckets, { "plyr.fm": 1_000 + sum(trafficDaily.filter((row) => row.domain === "plyr.fm"), "hits"), "old-app.example": 5 * trafficDaily.filter((row) => row.domain === "old-app.example").length });
  const tursoActors = 13_000_000 + sum(source.actor_deltas, "actors_delta");
  const d1Sources = sources(mirror.traffic_buckets);
  const d1Actors = sum(mirror.actor_deltas, "actors_delta");

  for (const name of RANGED_NAMES) {
    await load(turso, name, RANGED_TABLES[name].columns, source[name]);
    await load(d1, name, RANGED_TABLES[name].columns, mirror[name]);
  }
  await load(turso, "traffic_daily", DAILY_TABLES.traffic_daily.columns, trafficDaily);
  await load(turso, "request_daily", DAILY_TABLES.request_daily.columns, requestDaily);
  await load(turso, "traffic_sources", ["domain", "hits"], tursoSources);
  await load(d1, "traffic_sources", ["domain", "hits"], d1Sources);
  await turso.batch([
    turso.prepare("UPDATE counters SET value = ?1 WHERE name = 'actors_total'").bind(tursoActors),
    turso.prepare("INSERT INTO counters (name, value) VALUES (?1, ?2)").bind(LABEL_CURSOR.name, LABEL_CURSOR.value),
  ]);
  await d1.prepare("UPDATE counters SET value = ?1 WHERE name = 'actors_total'").bind(d1Actors).run();

  return {
    turso: { ...source, traffic_daily: trafficDaily, request_daily: requestDaily, traffic_sources: tursoSources, actors_total: tursoActors },
    d1: { ...mirror, traffic_sources: d1Sources, actors_total: d1Actors },
  };
}

/** What D1 must hold after a backfill at `cut`, worked out from the seeded
 *  rows alone: Turso's rows before the cut, D1's own from it, and the two
 *  totals as Turso had them at the cut plus D1's count since. */
export function backfilled(seeded: Seeded, cut = CUT) {
  const merged = (name: RangedName) => {
    const { key, bucketsPerKey, primaryKey } = RANGED_TABLES[name];
    const end = Math.floor(cut / bucketsPerKey);
    const byKey = (a: StatRow, b: StatRow) => {
      const column = primaryKey.find((name) => a[name] !== b[name]);
      return column === undefined ? 0 : a[column]! < b[column]! ? -1 : 1;
    };
    return [
      ...seeded.turso[name].filter((row) => Number(row[key]) < end),
      ...seeded.d1[name].filter((row) => Number(row[key]) >= end),
    ].sort(byKey);
  };
  const since = (rows: StatRow[], column: string, domain?: string) =>
    sum(rows.filter((row) => Number(row.bucket) >= cut && (domain === undefined || row.domain === domain)), column);
  const domains = new Set([
    ...seeded.turso.traffic_sources.map((row) => String(row.domain)),
    ...seeded.d1.traffic_buckets.filter((row) => Number(row.bucket) >= cut).map((row) => String(row.domain)),
  ]);
  const trafficSources = [...domains].sort().map((domain) => ({
    domain,
    hits: Number(seeded.turso.traffic_sources.find((row) => row.domain === domain)?.hits ?? 0)
      - since(seeded.turso.traffic_buckets, "hits", domain) + since(seeded.d1.traffic_buckets, "hits", domain),
  }));
  return {
    actor_deltas: merged("actor_deltas"),
    counters: [{ name: "actors_total", value: seeded.turso.actors_total - since(seeded.turso.actor_deltas, "actors_delta") + since(seeded.d1.actor_deltas, "actors_delta") }],
    metrics: merged("metrics"),
    request_buckets: merged("request_buckets"),
    request_daily: seeded.turso.request_daily,
    snapshots: merged("snapshots"),
    traffic_buckets: merged("traffic_buckets"),
    traffic_daily: seeded.turso.traffic_daily,
    traffic_sources: trafficSources,
  };
}

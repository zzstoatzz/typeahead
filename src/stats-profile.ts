import { z } from "zod";
import { rowsAs, type SqlDb } from "./db";

/** What each UTC hour of the day usually looks like, so /stats can draw a
 *  typical range behind the live line. Built hourly by the cron from the last
 *  PROFILE_DAYS of buckets, ending a day back so the window being drawn is not
 *  compared with itself. Two grains, because a 5-minute bucket and an hour
 *  vary differently: the 1h and 24h views read `m5`, 7d and 30d read `h`.
 *  `source` follows traffic_buckets (the per-app chart's total); `requests`
 *  follows request_buckets (the endpoint and status views' total). */
export const PROFILE_KEY = "stats_profile";
export const PROFILE_DAYS = 28;

const band = z.object({ p10: z.number(), p25: z.number(), p50: z.number(), p75: z.number(), p90: z.number() });
const grains = z.object({ m5: z.array(band).length(24), h: z.array(band).length(24) });
const profileSchema = z.object({
  version: z.literal(1),
  as_of: z.string(),
  days: z.number().int().positive(),
  source: grains,
  requests: grains,
});
export type Band = z.infer<typeof band>;
export type StatsProfile = z.infer<typeof profileSchema>;

export function parseProfile(raw: string | null): StatsProfile | null {
  if (raw === null) return null;
  try {
    const parsed = profileSchema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

/** linear interpolation between closest ranks; `sorted` ascending, non-empty */
export function quantile(sorted: number[], p: number): number {
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.min(lo + 1, sorted.length - 1);
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (i - lo);
}

/** Bands per UTC hour from per-bucket totals over [start, end) in 5-minute
 *  buckets. Missing buckets count as zero: a quiet bucket is a real reading. */
export function hourOfDayBands(totals: Map<number, number>, start: number, end: number, grain: "m5" | "h"): Band[] {
  const samples: number[][] = Array.from({ length: 24 }, () => []);
  if (grain === "m5") {
    for (let b = start; b < end; b++) samples[Math.floor(b / 12) % 24]!.push(totals.get(b) ?? 0);
  } else {
    for (let hour = Math.ceil(start / 12); (hour + 1) * 12 <= end; hour++) {
      let sum = 0;
      for (let b = hour * 12; b < hour * 12 + 12; b++) sum += totals.get(b) ?? 0;
      samples[hour % 24]!.push(sum);
    }
  }
  return samples.map((xs) => {
    const sorted = xs.length ? [...xs].sort((a, b) => a - b) : [0];
    const q = (p: number) => Math.round(quantile(sorted, p) * 10) / 10;
    return { p10: q(0.1), p25: q(0.25), p50: q(0.5), p75: q(0.75), p90: q(0.9) };
  });
}

type ProfileKv = { put(key: string, value: string): Promise<void> };

/** the profile as the cron computes it, read from one statistics store */
export async function readProfile(db: SqlDb, now = Date.now()): Promise<StatsProfile> {
  const end = Math.floor(now / 3_600_000) * 12 - 288;
  const start = end - PROFILE_DAYS * 288;
  const [trafficRes, requestRes] = await db.batch([
    db.prepare("SELECT bucket, SUM(hits) AS hits FROM traffic_buckets WHERE bucket >= ?1 AND bucket < ?2 GROUP BY bucket").bind(start, end),
    db.prepare("SELECT bucket, SUM(hits) AS hits FROM request_buckets WHERE bucket >= ?1 AND bucket < ?2 GROUP BY bucket").bind(start, end),
  ], "read");
  const totals = (rows: { bucket: number; hits: number }[]) => new Map(rows.map((r) => [Number(r.bucket), Number(r.hits)]));
  const traffic = totals(rowsAs<{ bucket: number; hits: number }>(trafficRes?.results ?? []));
  const requests = totals(rowsAs<{ bucket: number; hits: number }>(requestRes?.results ?? []));
  return {
    version: 1,
    as_of: new Date(now).toISOString(),
    days: PROFILE_DAYS,
    source: { m5: hourOfDayBands(traffic, start, end, "m5"), h: hourOfDayBands(traffic, start, end, "h") },
    requests: { m5: hourOfDayBands(requests, start, end, "m5"), h: hourOfDayBands(requests, start, end, "h") },
  } satisfies StatsProfile;
}

export async function recordProfile(db: SqlDb, kv: ProfileKv, now = Date.now()): Promise<StatsProfile> {
  const profile = await readProfile(db, now);
  await kv.put(PROFILE_KEY, JSON.stringify(profile));
  return profile;
}

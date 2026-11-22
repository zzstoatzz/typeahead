import * as z from "zod/mini";
import type { SqlDb } from "./db";
import { handleSearch } from "./handlers/search";
import type { Env } from "./types";

/** The app name /first's searches are counted under on /stats. */
export const FIRST_CLIENT = "typeahead-first";
export const LETTERS = [..."abcdefghijklmnopqrstuvwxyz"] as const;
/** Queries whose first place is kept for every day. Any other query is
 *  answered live and kept nowhere, so a visitor cannot grow the table. */
export const TRACKED: readonly string[] = [...LETTERS];
/** Places kept per query per day. */
export const KEPT = 3;
/** Places shown for one query. */
export const SHOWN = 5;
export const HISTORY_DAYS = 30;
export const MAX_QUERY = 64;
const DAY_MS = 86_400_000;
const ORIGIN = "https://typeahead.waow.tech";

export interface Placed {
  did: string;
  handle: string;
  displayName: string;
  avatar: string;
}

/** One live search. `ours` is false when the answer came from the Bluesky
 *  fallback: it is shown, but it is not our ranking, so it is never kept. */
export type Live =
  | { query: string; ok: true; ours: boolean; actors: Placed[] }
  | { query: string; ok: false };

export interface Day {
  day: number;
  first: Placed;
}

export const dayOf = (ms: number) => Math.floor(ms / DAY_MS);

/** A path segment as a query: decoded, trimmed, lower-cased, at most
 *  `MAX_QUERY` characters; null when nothing is left or it does not decode. */
export function parseQuery(segment: string): string | null {
  let decoded: string;
  try { decoded = decodeURIComponent(segment); } catch { return null; }
  const query = decoded.trim().toLowerCase();
  return query && query.length <= MAX_QUERY ? query : null;
}

export const tracked = (query: string) => TRACKED.includes(query);

const answerSchema = z.object({
  actors: z.array(z.object({
    did: z.string(),
    handle: z.string(),
    displayName: z.optional(z.string()),
    avatar: z.optional(z.string()),
  })),
});

/** Searches one render or cron run has open at once. A Worker invocation
 *  holds six outbound connections; more queue behind them while their
 *  timeouts run, and a search that times out is answered by the Bluesky
 *  fallback, which is not kept. */
export const SEARCHES_AT_ONCE = 6;

/** Ask the Worker's own search for each query, as the API would answer it:
 *  same moderation, cache and fallback, counted under `FIRST_CLIENT`. Answers
 *  are in the order of `queries`. */
export async function searchLive(env: Env, ctx: ExecutionContext, queries: readonly string[], limit: number): Promise<Live[]> {
  const one = async (query: string): Promise<Live> => {
    const url = `${ORIGIN}/xrpc/tech.waow.typeahead.searchActors?q=${encodeURIComponent(query)}&limit=${limit}`;
    try {
      const response = await handleSearch(new Request(url, { headers: { "X-Client": FIRST_CLIENT } }), env, ctx);
      if (!response.ok) return { query, ok: false };
      const answer = answerSchema.parse(await response.json());
      const actors = answer.actors.map((actor) => ({ did: actor.did, handle: actor.handle, displayName: actor.displayName ?? "", avatar: actor.avatar ?? "" }));
      return { query, ok: true, ours: response.headers.get("X-Typeahead-Engine") !== "bluesky", actors };
    } catch {
      return { query, ok: false };
    }
  };
  const lives: Live[] = [];
  let next = 0;
  const worker = async () => {
    while (next < queries.length) {
      const i = next++;
      // SAFETY: `i` is below `queries.length`, checked on the line above
      lives[i] = await one(queries[i]!);
    }
  };
  await Promise.all(Array.from({ length: Math.min(SEARCHES_AT_ONCE, queries.length) }, worker));
  return lives;
}

/** Tracked queries with no row for `day`. One read. */
export async function missing(db: SqlDb, day: number, queries: readonly string[]): Promise<string[]> {
  const { results } = await db.prepare("SELECT DISTINCT query FROM first_daily WHERE day = ?").bind(day).all<{ query: string }>();
  const kept = new Set(results.map((row) => row.query));
  return queries.filter((query) => tracked(query) && !kept.has(query));
}

/** Keep `day`'s places for every tracked query among `lives` that our own
 *  search answered. A day already kept is left as it is. */
export async function keep(db: SqlDb, day: number, lives: readonly Live[]): Promise<number> {
  const rows = lives.flatMap((live) => live.ok && live.ours && tracked(live.query)
    ? live.actors.slice(0, KEPT).map((actor, i) => db.prepare(
      "INSERT OR IGNORE INTO first_daily (day, query, rank, did, handle, display_name, avatar) VALUES (?, ?, ?, ?, ?, ?, ?)",
    ).bind(day, live.query, i + 1, actor.did, actor.handle, actor.displayName, actor.avatar))
    : []);
  const unkept = lives.filter((live) => tracked(live.query) && !(live.ok && live.ours))
    .map((live) => ({ query: live.query, reason: live.ok ? "fallback" : "failed" }));
  if (unkept.length) console.log(JSON.stringify({ event: "first_not_kept", day, unkept }));
  if (!rows.length) return 0;
  await db.batch(rows, "write");
  return rows.length;
}

/** The hourly cron's share: search and keep today's tracked queries that the
 *  page has not kept yet. Usually one read and nothing else. */
export async function keepToday(env: Env, ctx: ExecutionContext, db: SqlDb, now = Date.now()): Promise<number> {
  const day = dayOf(now);
  const queries = await missing(db, day, TRACKED);
  if (!queries.length) return 0;
  return keep(db, day, await searchLive(env, ctx, queries, KEPT));
}

/** First place per day for `queries` over the last `HISTORY_DAYS` before
 *  `today`, most recent first. */
export async function history(db: SqlDb, today: number, queries: readonly string[]): Promise<Map<string, Day[]>> {
  const wanted = queries.filter(tracked);
  const out = new Map<string, Day[]>(wanted.map((query) => [query, []]));
  if (!wanted.length) return out;
  const { results } = await db.prepare(
    `SELECT day, query, did, handle, display_name, avatar FROM first_daily
     WHERE rank = 1 AND day >= ? AND day < ? AND query IN (${wanted.map(() => "?").join(", ")})
     ORDER BY day DESC`,
  ).bind(today - HISTORY_DAYS, today, ...wanted).all<{ day: number; query: string; did: string; handle: string; display_name: string; avatar: string }>();
  for (const row of results) {
    out.get(row.query)?.push({ day: row.day, first: { did: row.did, handle: row.handle, displayName: row.display_name, avatar: row.avatar } });
  }
  return out;
}

/** Days in a row, ending today, that `first` has been first: today, then
 *  each earlier kept day it also held. A day not kept ends the run. */
export function streak(first: Placed, today: number, days: readonly Day[]): number {
  const held = new Map(days.map((entry) => [entry.day, entry.first.did]));
  let count = 1;
  while (held.get(today - count) === first.did) count++;
  return count;
}

/** One slot of the strip: who was first that day, or nobody kept it. */
export type Slot = { day: number; first: Placed | null };

/** The last `HISTORY_DAYS` days, oldest first, ending with today's live first. */
export function strip(first: Placed | null, today: number, days: readonly Day[]): Slot[] {
  const byDay = new Map(days.map((entry) => [entry.day, entry.first]));
  return Array.from({ length: HISTORY_DAYS }, (_, i) => {
    const day = today - (HISTORY_DAYS - 1) + i;
    return { day, first: day === today ? first : byDay.get(day) ?? null };
  });
}

export interface Reign {
  first: Placed | null;
  days: number;
}

/** The strip as runs, most recent first: who held it and for how long, with
 *  unkept days as runs of nobody. */
export function reigns(slots: readonly Slot[]): Reign[] {
  const out: Reign[] = [];
  for (const slot of [...slots].reverse()) {
    const last = out.at(-1);
    if (last && last.first?.did === slot.first?.did) last.days++;
    else out.push({ first: slot.first, days: 1 });
  }
  return out;
}

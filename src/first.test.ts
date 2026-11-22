import { afterAll, beforeAll, expect, test } from "bun:test";
import { HISTORY_DAYS, KEPT, type Live, type Placed, dayOf, history, keep, missing, parseQuery, reigns, streak, strip } from "./first";
import { statsStores } from "./stats/stats-d1-fixture";

const actor = (name: string): Placed => ({ did: `did:plc:${name}`, handle: `${name}.test`, displayName: name, avatar: "" });
const ours = (query: string, names: string[]): Live => ({ query, ok: true, ours: true, actors: names.map(actor) });
const today = 20_000;

let stores: Awaited<ReturnType<typeof statsStores>>;
beforeAll(async () => { stores = await statsStores(); });
afterAll(async () => { await stores.dispose(); });

test("a query is decoded, trimmed and lower-cased, and refused when empty, undecodable or long", () => {
  expect(parseQuery("Nate")).toBe("nate");
  expect(parseQuery("%20Bill%20")).toBe("bill");
  expect(parseQuery("%E0%A4%A")).toBeNull();
  expect(parseQuery("%20")).toBeNull();
  expect(parseQuery("x".repeat(65))).toBeNull();
  expect(parseQuery("x".repeat(64))).toBe("x".repeat(64));
});

test("a day is kept once, for tracked queries our own search answered, and only its first places", async () => {
  const { db } = stores;
  expect(await missing(db, today, ["a", "b", "nate"])).toEqual(["a", "b"]);
  const fallback: Live = { query: "b", ok: true, ours: false, actors: [actor("fallback")] };
  const written = await keep(db, today, [ours("a", ["ann", "amy", "abe", "art"]), fallback, ours("nate", ["nate"]), { query: "c", ok: false }]);
  expect(written).toBe(KEPT);
  expect(await missing(db, today, ["a", "b", "nate"])).toEqual(["b"]);

  await keep(db, today, [ours("a", ["zed"])]);
  const { results } = await db.prepare("SELECT rank, did FROM first_daily WHERE day = ? AND query = 'a' ORDER BY rank").bind(today).all<{ rank: number; did: string }>();
  expect(results).toEqual([{ rank: 1, did: "did:plc:ann" }, { rank: 2, did: "did:plc:amy" }, { rank: 3, did: "did:plc:abe" }]);
});

test("history reads first places before today within the window, most recent first", async () => {
  const { db } = stores;
  for (const [day, name] of [[today - 1, "ann"], [today - 2, "ann"], [today - 4, "bob"], [today - HISTORY_DAYS - 1, "old"]] as const) {
    await keep(db, day, [ours("d", [name])]);
  }
  const days = (await history(db, today, ["d", "nate"])).get("d") ?? [];
  expect(days.map((entry) => [today - entry.day, entry.first.handle])).toEqual([[1, "ann.test"], [2, "ann.test"], [4, "bob.test"]]);
  expect((await history(db, today, ["nate"])).has("nate")).toBe(false);
});

test("a streak counts today and each earlier day held, and a day not kept ends it", () => {
  const ann = actor("ann");
  const held = (back: number, name: string) => ({ day: today - back, first: actor(name) });
  expect(streak(ann, today, [])).toBe(1);
  expect(streak(ann, today, [held(1, "ann"), held(2, "ann"), held(3, "bob")])).toBe(3);
  expect(streak(ann, today, [held(1, "ann"), held(3, "ann")])).toBe(2);
  expect(streak(ann, today, [held(1, "bob")])).toBe(1);
});

test("the strip ends with today's live first, and its runs read most recent first", () => {
  const days = [{ day: today - 1, first: actor("ann") }, { day: today - 2, first: actor("bob") }, { day: today - 3, first: actor("bob") }];
  const slots = strip(actor("ann"), today, days);
  expect(slots).toHaveLength(HISTORY_DAYS);
  expect(slots.at(-1)).toEqual({ day: today, first: actor("ann") });
  expect(slots.at(-5)).toEqual({ day: today - 4, first: null });
  expect(reigns(slots).map((run) => [run.first?.handle ?? null, run.days])).toEqual([
    ["ann.test", 2], ["bob.test", 2], [null, HISTORY_DAYS - 4],
  ]);
});

test("days are UTC", () => {
  expect(dayOf(Date.UTC(2026, 10, 21, 23, 59))).toBe(dayOf(Date.UTC(2026, 10, 21, 0, 0)));
  expect(dayOf(Date.UTC(2026, 10, 22))).toBe(dayOf(Date.UTC(2026, 10, 21)) + 1);
});

import { afterAll, afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { STORE_COUNT_MAX_AGE_S, recordExclusions, recordSnapshot, slotExclusions, slotSnapshot } from "./corpus";
import type { SqlDb } from "./db";
import { EXCLUSIONS_KEY, EXCLUSIONS_REFRESH_MS, parseExclusions, type SearchExclusions } from "./exclusions";
import type { SlotCorpus, SlotEnv } from "./slot-read";
import { statsRows, statsStores } from "./stats-d1-fixture";

// The corpus numbers on /stats under CORPUS_MODE: Turso's counts, the routed
// slot's, or Turso's with the slot's logged beside them.

const stores = await statsStores();
afterAll(() => stores.dispose());
await stores.client.executeMultiple(`
  CREATE TABLE actors (did TEXT PRIMARY KEY, handle TEXT NOT NULL DEFAULT '', avatar_url TEXT DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0, labels TEXT NOT NULL DEFAULT '[]');
  CREATE TABLE mod_overrides (did TEXT PRIMARY KEY, action TEXT NOT NULL);
  CREATE TABLE blocked_domains (domain TEXT PRIMARY KEY);
  INSERT INTO actors (did, handle, avatar_url, hidden) VALUES ('did:plc:a', 'a.test', 'cid', 0), ('did:plc:b', '', '', 0), ('did:plc:c', 'handle.invalid', '', 0);
  INSERT INTO mod_overrides (did, action) VALUES ('did:plc:a', 'hide');
  UPDATE counters SET value = 3 WHERE name = 'actors_total';
`);

const NOW = Date.UTC(2026, 10, 12, 9, 30);
const HOUR = Math.floor(NOW / 3_600_000);
const CORPUS: SlotCorpus = {
  index: { actors: 1000, searchable: 950, handle_invalid: 30, quarantined: 7 },
  store: { live: 1200, total: 1180, with_handles: 1000, with_avatars: 600, hidden: 20, scanned_at: NOW / 1000 - 600 },
};
const TURSO_SNAPSHOT = { hour: HOUR, total: 3, with_handles: 2, with_avatars: 1, hidden: 0 };
const SLOT_SNAPSHOT = { hour: HOUR, total: 1180, with_handles: 1000, with_avatars: 600, hidden: 20 };

function slot(answer: () => Response) {
  const asked: string[] = [];
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      asked.push(String(input));
      return answer();
    },
  };
  // SAFETY: the reads use the binding only through fetch(url, init).
  const env: SlotEnv = { SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport as Fetcher };
  return { env, asked };
}
const answering = (corpus: SlotCorpus) => slot(() => Response.json(corpus));
const down = () => slot(() => Response.json({ error: "not ready" }, { status: 503 }));

/** Turso's statistics tables stay writable; its actor tables may not be read. */
const statsOnly: SqlDb = {
  prepare(sql) {
    if (/\b(actors|counters|mod_overrides|blocked_domains)\b/.test(sql)) throw new Error(`Turso's actor tables were read: ${sql}`);
    return stores.turso.prepare(sql);
  },
  batch(stmts, mode) { return stores.turso.batch(stmts, mode); },
};

function memoryKv() {
  const store = new Map<string, string>();
  return {
    store,
    get: async (key: string) => store.get(key) ?? null,
    put: async (key: string, value: string) => { store.set(key, value); },
  };
}

const snapshots = async () => (await statsRows(stores.turso)).find((entry) => entry.table === "snapshots")?.rows;
let logged: string[] = [];
let log: ReturnType<typeof spyOn>;
beforeEach(async () => {
  logged = [];
  log = spyOn(console, "log").mockImplementation((line: string) => { logged.push(line); });
  await stores.turso.prepare("DELETE FROM snapshots").run();
});
afterEach(() => { log.mockRestore(); });
const events = () => logged.map((line) => JSON.parse(line));

test("a slot's snapshot is the store's count, and there is none without a recent count", () => {
  expect(slotSnapshot(CORPUS, NOW)).toEqual(SLOT_SNAPSHOT);
  expect(slotSnapshot({ ...CORPUS, store: null }, NOW)).toBeNull();
  const aged = (age: number) => ({ ...CORPUS, store: { ...CORPUS.store!, scanned_at: NOW / 1000 - age } });
  expect(slotSnapshot(aged(STORE_COUNT_MAX_AGE_S), NOW)).toEqual(SLOT_SNAPSHOT);
  expect(slotSnapshot(aged(STORE_COUNT_MAX_AGE_S + 1), NOW)).toBeNull();
});

test("a slot's exclusions come from the index: unsearchable documents less the unverified handles", () => {
  expect(slotExclusions(CORPUS, new Date(NOW))).toEqual({
    version: 2, as_of: "2026-11-12T09:30:00.000Z", policy_hidden: 20, stale_hidden_flags: 0, handle_invalid: 30, quarantined: 7,
  });
  const allInvalid = { ...CORPUS, index: { actors: 10, searchable: 4, handle_invalid: 9, quarantined: 0 } };
  expect(slotExclusions(allInvalid, new Date(NOW)).policy_hidden).toBe(0);
});

test("with no mode the snapshot is Turso's count and the slot is not asked", async () => {
  for (const mode of [undefined, "turso", "d1"]) {
    const { env, asked } = answering(CORPUS);
    await recordSnapshot(stores.turso, { ...env, CORPUS_MODE: mode }, NOW);
    expect(await snapshots()).toEqual([TURSO_SNAPSHOT]);
    expect(asked).toEqual([]);
  }
});

test("slot mode records the slot's count without reading Turso's actors", async () => {
  const { env, asked } = answering(CORPUS);
  await recordSnapshot(statsOnly, { ...env, CORPUS_MODE: "slot" }, NOW);
  expect(asked).toEqual(["https://transport.test/corpus"]);
  expect(await snapshots()).toEqual([SLOT_SNAPSHOT]);
});

test("slot mode writes no row while the store has no count, and fails the step when the slot does not answer", async () => {
  await recordSnapshot(statsOnly, { ...answering({ ...CORPUS, store: null }).env, CORPUS_MODE: "slot" }, NOW);
  expect(await snapshots()).toEqual([]);
  expect(events()).toEqual([{ event: "corpus_snapshot_skipped", reason: "the slot's store has no recent count" }]);

  await expect(recordSnapshot(statsOnly, { ...down().env, CORPUS_MODE: "slot" }, NOW)).rejects.toThrow("the slot answered 503");
  expect(await snapshots()).toEqual([]);
});

test("compare mode records Turso's count and logs the slot's beside it, or that the slot failed", async () => {
  await recordSnapshot(stores.turso, { ...answering(CORPUS).env, CORPUS_MODE: "compare" }, NOW);
  expect(await snapshots()).toEqual([TURSO_SNAPSHOT]);
  expect(events()).toEqual([{ event: "corpus_compare", what: "snapshot", turso: TURSO_SNAPSHOT, slot: SLOT_SNAPSHOT }]);

  logged = [];
  await recordSnapshot(stores.turso, { ...down().env, CORPUS_MODE: "compare" }, NOW);
  expect(await snapshots()).toEqual([TURSO_SNAPSHOT]);
  expect(events()).toEqual([{ event: "corpus_compare_failed", what: "snapshot", error: "the slot answered 503" }]);
});

test("exclusions follow the same three modes and are still counted once a day", async () => {
  const at = new Date(NOW);
  const tursoCount = { version: 2, as_of: at.toISOString(), policy_hidden: 1, stale_hidden_flags: 0, handle_invalid: 1, quarantined: 0 } satisfies SearchExclusions;
  const slotCount = slotExclusions(CORPUS, at);

  const tursoKv = memoryKv();
  const { env, asked } = answering(CORPUS);
  expect(await recordExclusions(stores.turso, tursoKv, env, at)).toEqual(tursoCount);
  expect(asked).toEqual([]);

  const slotKv = memoryKv();
  expect(await recordExclusions(statsOnly, slotKv, { ...env, CORPUS_MODE: "slot" }, at)).toEqual(slotCount);
  expect(parseExclusions(slotKv.store.get(EXCLUSIONS_KEY) ?? null)).toEqual(slotCount);
  expect(await recordExclusions(statsOnly, slotKv, { ...env, CORPUS_MODE: "slot" }, new Date(NOW + EXCLUSIONS_REFRESH_MS - 1))).toBeNull();
  expect(asked).toHaveLength(1);

  logged = [];
  const compareKv = memoryKv();
  expect(await recordExclusions(stores.turso, compareKv, { ...env, CORPUS_MODE: "compare" }, at)).toEqual(tursoCount);
  expect(events()).toEqual([{ event: "corpus_compare", what: "exclusions", turso: tursoCount, slot: slotCount }]);

  const failedKv = memoryKv();
  await expect(recordExclusions(statsOnly, failedKv, { ...down().env, CORPUS_MODE: "slot" }, at)).rejects.toThrow("the slot answered 503");
  expect(failedKv.store.size).toBe(0);
});

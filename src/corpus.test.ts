import { afterAll, afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { STORE_COUNT_MAX_AGE_S, recordExclusions, recordSnapshot, slotExclusions, slotSnapshot } from "./corpus";
import { EXCLUSIONS_KEY, EXCLUSIONS_REFRESH_MS, parseExclusions } from "./exclusions";
import type { SlotCorpus, SlotEnv } from "./slot-read";
import { statsRows, statsStores } from "./stats-d1-fixture";

// The corpus numbers on /stats come from the routed slot: the hourly
// snapshot from its store's count, the daily exclusions from its index.

const stores = await statsStores();
afterAll(() => stores.dispose());

const NOW = Date.UTC(2026, 10, 12, 9, 30);
const HOUR = Math.floor(NOW / 3_600_000);
const CORPUS: SlotCorpus = {
  index: { actors: 1000, searchable: 950, handle_invalid: 30, quarantined: 7 },
  store: { live: 1200, total: 1180, with_handles: 1000, with_avatars: 600, hidden: 20, scanned_at: NOW / 1000 - 600 },
};
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
  return { env: { ...env, STATS_DB: stores.d1 }, asked };
}
const answering = (corpus: SlotCorpus) => slot(() => Response.json(corpus));
const down = () => slot(() => Response.json({ error: "not ready" }, { status: 503 }));

function memoryKv() {
  const store = new Map<string, string>();
  return {
    store,
    get: async (key: string) => store.get(key) ?? null,
    put: async (key: string, value: string) => { store.set(key, value); },
  };
}

const snapshots = async () => (await statsRows(stores.db)).find((entry) => entry.table === "snapshots")?.rows;
let logged: string[] = [];
let log: ReturnType<typeof spyOn>;
beforeEach(async () => {
  logged = [];
  log = spyOn(console, "log").mockImplementation((line: string) => { logged.push(line); });
  await stores.db.prepare("DELETE FROM snapshots").run();
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

test("the hourly snapshot is the slot's count, written to the statistics store", async () => {
  const { env, asked } = answering(CORPUS);
  await recordSnapshot(env, NOW);
  expect(asked).toEqual(["https://transport.test/corpus"]);
  expect(await snapshots()).toEqual([SLOT_SNAPSHOT]);
});

test("no row is written while the store has no count, and a slot that does not answer fails the step", async () => {
  await recordSnapshot(answering({ ...CORPUS, store: null }).env, NOW);
  expect(await snapshots()).toEqual([]);
  expect(events()).toEqual([{ event: "corpus_snapshot_skipped", reason: "the slot's store has no recent count" }]);

  await expect(recordSnapshot(down().env, NOW)).rejects.toThrow("the slot answered 503");
  expect(await snapshots()).toEqual([]);
});

test("exclusions are the slot's, counted once a day", async () => {
  const at = new Date(NOW);
  const counted = slotExclusions(CORPUS, at);

  const kv = memoryKv();
  const { env, asked } = answering(CORPUS);
  expect(await recordExclusions(kv, env, at)).toEqual(counted);
  expect(parseExclusions(kv.store.get(EXCLUSIONS_KEY) ?? null)).toEqual(counted);
  expect(await recordExclusions(kv, env, new Date(NOW + EXCLUSIONS_REFRESH_MS - 1))).toBeNull();
  expect(asked).toHaveLength(1);

  const later = new Date(NOW + EXCLUSIONS_REFRESH_MS);
  expect(await recordExclusions(kv, env, later)).toEqual(slotExclusions(CORPUS, later));
  expect(asked).toHaveLength(2);
  expect(parseExclusions(kv.store.get(EXCLUSIONS_KEY) ?? null)?.as_of).toBe(later.toISOString());

  const failedKv = memoryKv();
  await expect(recordExclusions(failedKv, down().env, at)).rejects.toThrow("the slot answered 503");
  expect(failedKv.store.size).toBe(0);
});

import { afterEach, expect, setSystemTime, test } from "bun:test";
import { IDLE_AFTER_MS, ROUTE_LIFETIME_MS, RouteCache } from "./route-cache";
import { readRoute, ROUTE_KEY, type RouteSource } from "./search-route";

const current = {
  engine: "tantivy",
  origin: "https://typeahead-slot-b.fly.dev",
  build_id: "b1792908655-test",
  format: "tantivy-0.27-f32-did-expiry-v4",
  admission_sha256: "a".repeat(64),
  binary_sha256: "c".repeat(64),
  retrieval_strategy: "per-segment",
} as const;

/** a stored routing record, served as R2 would return its text */
function bucket(body = JSON.stringify({ version: 1, current })) {
  const reads: string[] = [];
  let release: (() => void) | undefined;
  let held = false;
  return {
    reads,
    hold() {
      held = true;
    },
    release() {
      held = false;
      release?.();
    },
    async get(key: string) {
      reads.push(key);
      if (held) await new Promise<void>((resolve) => (release = resolve));
      // SAFETY: route readers use only `size` and `json()` of the object
      return { size: body.length, json: async () => JSON.parse(body) } as R2ObjectBody;
    },
  };
}

afterEach(() => setSystemTime());

test("the shared copy is served from memory within its lifetime and re-read after it", async () => {
  let now = 1_000_000;
  const r2 = bucket();
  const cache = new RouteCache(r2, () => now);
  expect((await cache.current()).route).toEqual(current);
  now += ROUTE_LIFETIME_MS - 1;
  expect((await cache.current()).validatedAt).toBe(1_000_000);
  expect(r2.reads).toEqual([ROUTE_KEY]);
  now += 1;
  expect((await cache.current()).validatedAt).toBe(now);
  expect(r2.reads.length).toBe(2);
});

test("concurrent searches share one R2 read", async () => {
  const r2 = bucket();
  r2.hold();
  const cache = new RouteCache(r2);
  const waiting = [cache.current(), cache.current(), cache.refresh()];
  r2.release();
  const results = await Promise.all(waiting);
  expect(r2.reads.length).toBe(1);
  expect(new Set(results).size).toBe(1);
});

test("an invalid record is not served", async () => {
  const cache = new RouteCache(bucket(JSON.stringify({ version: 1, current: { ...current, origin: "https://example.com" } })));
  await expect(cache.current()).rejects.toThrow();
});

test("the refresher goes idle once searches stop", async () => {
  let now = 1_000_000;
  const cache = new RouteCache(bucket(), () => now);
  await cache.current();
  now += IDLE_AFTER_MS - 1;
  expect(cache.active()).toBe(true);
  now += 1;
  expect(cache.active()).toBe(false);
});

test("the transport uses a fresh shared route without reading R2", async () => {
  setSystemTime(new Date(2_000_000_000_000));
  const r2 = bucket();
  const source: RouteSource = { current: async () => ({ route: current, validatedAt: Date.now() - 200 }) };
  expect(await readRoute(r2, true, source)).toEqual(current);
  expect(r2.reads).toEqual([]);
});

test("the transport reads R2 itself when the shared route fails, hangs, is too old or is invalid", async () => {
  const sources: RouteSource[] = [
    { current: async () => Promise.reject(new Error("unavailable")) },
    { current: () => new Promise(() => {}) },
    { current: async () => ({ route: current, validatedAt: Date.now() - ROUTE_LIFETIME_MS }) },
    // a record that would not pass the transport's own validation
    { current: async () => ({ route: { ...current, build_id: "../../other" }, validatedAt: Date.now() }) },
  ];
  let at = 2_100_000_000_000;
  for (const source of sources) {
    at += 10 * ROUTE_LIFETIME_MS;
    setSystemTime(new Date(at));
    const r2 = bucket();
    expect(await readRoute(r2, true, source)).toEqual(current);
    expect(r2.reads).toEqual([ROUTE_KEY]);
  }
});

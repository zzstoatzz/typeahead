import { expect, test } from "bun:test";
import { DAILY_BUDGET, GAP_MS, cacheState, cooldown, nextAllowed, pairs, summarize, type Observation } from "./model";
import { sample } from "./sampling";

export function observation(fields: Partial<Observation> = {}): Observation {
  return { id: "test", run: "run", corpus: "v1", provider: "typeahead", q: "alice.test", category: "exact handle", round: 0,
    startedAt: 100, finishedAt: 200, ms: 100, outcome: "ok", status: 200,
    actors: [{ did: "did:plc:alice", handle: "alice.test", displayName: "Alice" }], headers: {}, error: null, cooldownUntil: 0, ...fields };
}

test("failures and interrupted attempts stay out of successful latency and completeness denominators", () => {
  const s = summarize([observation(), observation({ outcome: "http-error", status: 503, ms: 9000, actors: [] }),
    observation({ outcome: "pending", ms: null, finishedAt: null, actors: [] })]);
  expect(s).toMatchObject({ attempts: 3, completed: 2, ok: 1, failures: 1, pending: 1, averageMs: 100, successPct: 50, exactPct: 100, displayPct: 100, avatarPct: 0 });
  expect(summarize([]).medianMs).toBeNull();
});

test("pairs use DID identity, exclude fallbacks and don't turn two empty results into perfect overlap", () => {
  const a = observation();
  const b = observation({ provider: "bluesky", actors: [{ did: "did:plc:alice", handle: "renamed.test" }, { did: "did:plc:bob", handle: "bob.test" }] });
  expect(pairs([a, b])[0]).toMatchObject({ shared: 1, overlap: 50, sameFirst: true });
  expect(pairs([observation({ headers: { "x-typeahead-engine": "bluesky" } }), b])).toEqual([]);
  expect(pairs([a, { ...b, round: 1 }])).toEqual([]);
  expect(pairs([{ ...a, actors: [] }, { ...b, actors: [] }])[0].overlap).toBeNull();
});

test("budget includes pending requests, survives reruns and spaces both providers", () => {
  const now = 100_000;
  const history = Array.from({ length: DAILY_BUDGET }, (_, i) => observation({ startedAt: now - 1000 + i, outcome: "pending", finishedAt: null }));
  expect(nextAllowed(history, "typeahead", now)).toBe(now - 1000 + 86_400_000);
  expect(nextAllowed(history, "bluesky", now)).toBe(history.at(-1)!.startedAt + GAP_MS);
  expect(nextAllowed([observation({ cooldownUntil: now + 9999 })], "typeahead", now)).toBe(now + 9999);
});

test("429 honors both Retry-After formats and reset without shrinking the minimum cooldown", () => {
  const now = Date.UTC(2026, 10, 16);
  expect(cooldown(429, new Headers({ "Retry-After": "7200" }), now)).toBe(now + 7_200_000);
  expect(cooldown(429, new Headers({ "Retry-After": new Date(now + 7_200_000).toUTCString() }), now)).toBe(now + 7_200_000);
  expect(cooldown(429, new Headers({ "Retry-After": "garbage" }), now)).toBe(now + 3_600_000);
  expect(cooldown(200, new Headers({ "ratelimit-remaining": "0", "ratelimit-reset": String((now + 7_200_000) / 1000) }), now)).toBe(now + 7_200_000);
});

test("cache observations distinguish unknown from miss", () => {
  expect(cacheState(observation())).toBe("unknown");
  expect(cacheState(observation({ headers: { "server-timing": 'cache;desc="hit";dur=1' } }))).toBe("hit");
  expect(cacheState(observation({ headers: { "cdn-cache": "MISS" } }))).toBe("miss");
});

test("collector sends one identified request, preserves backoff and validates actors", async () => {
  let calls = 0;
  const result = await sample(observation(), async (url, init) => {
    calls++;
    expect(new URL(url).searchParams.get("limit")).toBe("10");
    expect(new Headers(init.headers).get("X-Client")).toBe("bench");
    expect(init.redirect).toBe("error");
    return new Response(null, { status: 429, headers: { "Retry-After": "7200" } });
  });
  expect(calls).toBe(1);
  expect(result.outcome).toBe("http-error");
  expect(result.cooldownUntil).toBeGreaterThan(Date.now() + 7_100_000);
  const invalid = await sample(observation(), async () => Response.json({ actors: [observation().actors[0], observation().actors[0]] }));
  expect(invalid.outcome).toBe("invalid-response");
  const failed = await sample(observation(), async () => { throw new Error("offline"); });
  expect(failed.outcome).toBe("network-error");
});

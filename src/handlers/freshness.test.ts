import { describe, expect, test, afterEach } from "bun:test";
import { measureFreshness } from "./freshness";
import type { Env } from "../types";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

/** stand in for the search service */
function backend(opts: {
  lag?: number; ready?: boolean; buildEpoch?: number; findsSelf?: boolean; down?: boolean;
}) {
  globalThis.fetch = (async (input: any) => {
    if (opts.down) throw new Error("unreachable");
    const url = String(input);
    const body = url.includes("/health")
      ? { status: "ok", ready: opts.ready ?? true, sync_lag_s: opts.lag ?? 42 }
      : url.includes("/index/status")
      ? { build_id: `b${opts.buildEpoch ?? Math.floor(Date.now() / 1000)}-21fc` }
      : { actors: opts.findsSelf === false ? [] : [{ handle: "zzstoatzz.io" }] };
    return new Response(JSON.stringify(body), { status: 200 });
  }) as any;
}

const env = { SEARCH_BACKEND_URL: "https://backend.test" } as unknown as Env;

describe("freshness", () => {
  test("a healthy pipeline is not stale", async () => {
    backend({ lag: 120 });
    const f = await measureFreshness(env);
    expect(f.stale).toBe(false);
    expect(f.ok).toBe(true);
    expect(f.replica_lag_s).toBe(120);
  });

  test("catches the 2026-07-28 failure: replica far behind, everything else green", async () => {
    // exactly the shape of the incident — service up, ready, snapshot fresh,
    // serving fine, and 48h stale underneath
    backend({ lag: 175469 });
    const f = await measureFreshness(env);
    expect(f.stale).toBe(true);
    expect(f.reasons.join(" ")).toContain("behind turso");
  });

  test("normal sync jitter does not trip it", async () => {
    backend({ lag: 8 * 60 });          // under the 10 min warn line
    expect((await measureFreshness(env)).stale).toBe(false);
  });

  test("a stalled snapshot rebuild is stale even with a healthy replica", async () => {
    backend({ lag: 30, buildEpoch: Math.floor(Date.now() / 1000) - 5 * 24 * 3600 });
    const f = await measureFreshness(env);
    expect(f.stale).toBe(true);
    expect(f.reasons.join(" ")).toContain("old");
  });

  test("a serving path that cannot find the probe handle is not ok", async () => {
    backend({ lag: 30, findsSelf: false });
    const f = await measureFreshness(env);
    expect(f.ok).toBe(false);
    expect(f.stale).toBe(true);
  });

  test("an unreachable backend is reported, not thrown", async () => {
    backend({ down: true });
    const f = await measureFreshness(env);
    expect(f.ok).toBe(false);
    expect(f.reasons.join(" ")).toContain("unreachable");
  });
});

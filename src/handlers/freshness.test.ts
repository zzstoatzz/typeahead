import { describe, expect, test, afterEach } from "bun:test";
import { measureFreshness, notifyFreshnessChange } from "./freshness";
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

describe("freshness notification", () => {
  const calls: any[] = [];
  const kvWith = (prev: any) => ({
    get: async () => prev,
  }) as unknown as KVNamespace;
  const envWith = (prev: any) => ({
    SEARCH_BACKEND_URL: "https://backend.test",
    ALERT_WEBHOOK_URL: "https://hook.test/x",
    KV: kvWith(prev),
  } as unknown as Env);

  const capture = () => {
    calls.length = 0;
    globalThis.fetch = (async (_u: any, init: any) => {
      calls.push(JSON.parse(init.body).content);
      return new Response("{}", { status: 200 });
    }) as any;
  };

  const F = (stale: boolean) => ({
    ok: !stale, stale, replica_lag_s: stale ? 3600 : 60, snapshot_age_s: 7200,
    build_id: "b1-x", probe_ok: true, reasons: stale ? ["replica 3600s behind turso"] : [],
  });

  test("fires when it goes stale", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: false }), F(true) as any);
    expect(calls.length).toBe(1);
    expect(calls[0]).toContain("serving is stale");
  });

  test("fires once on recovery, not on every healthy run", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: true }), F(false) as any);
    expect(calls[0]).toContain("fresh again");
    capture();
    await notifyFreshnessChange(envWith({ stale: false }), F(false) as any);
    expect(calls.length).toBe(0);          // steady healthy -> silent
  });

  test("does not repeat while it stays broken", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: true }), F(true) as any);
    expect(calls.length).toBe(0);
  });

  test("silent when no webhook is configured", async () => {
    capture();
    const env = { SEARCH_BACKEND_URL: "x", KV: kvWith(null) } as unknown as Env;
    await notifyFreshnessChange(env, F(true) as any);
    expect(calls.length).toBe(0);
  });
});

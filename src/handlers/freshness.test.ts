import { describe, expect, test, afterEach } from "bun:test";
import { measureFreshness, notifyFreshnessChange, type Freshness } from "./freshness";
import type { Env } from "../types";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

type FetchLike = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

function installFetch(fake: FetchLike): void {
  globalThis.fetch = Object.assign(fake, { preconnect: realFetch.preconnect });
}

/** stand in for the search service */
function backend(opts: {
  lag?: number; ready?: boolean; buildEpoch?: number; findsSelf?: boolean; down?: boolean;
}) {
  installFetch(async (input) => {
    if (opts.down) throw new Error("unreachable");
    const url = String(input);
    const body = url.includes("/health")
      ? { status: "ok", ready: opts.ready ?? true, sync_lag_s: opts.lag ?? 42 }
      : url.includes("/index/status")
      ? { build_id: `b${opts.buildEpoch ?? Math.floor(Date.now() / 1000)}-21fc` }
      : { actors: opts.findsSelf === false ? [] : [{ handle: "zzstoatzz.io" }] };
    return new Response(JSON.stringify(body), { status: 200 });
  });
}

type PrevState = { stale: boolean } | null;
/** the slice of KVNamespace notifyFreshnessChange consumes: one get("freshness", "json") */
type FreshnessKv = { get(key: string, type: "json"): Promise<PrevState> };

function fakeEnv(fields: { SEARCH_BACKEND_URL?: string; ALERT_WEBHOOK_URL?: string; KV?: FreshnessKv }): Env {
  // SAFETY: measureFreshness/notifyFreshnessChange read only SEARCH_BACKEND_URL,
  // ALERT_WEBHOOK_URL and KV.get(key, "json"); every test supplies the fields
  // its path touches, and the KV fake answers the only call shape that is made.
  return fields as Env;
}

const env = fakeEnv({ SEARCH_BACKEND_URL: "https://backend.test" });

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

  test("legacy and compact snapshots retain age alerts through real HTTP", async () => {
    const now = Math.floor(Date.now() / 1000);
    let buildId = "";
    const server = Bun.serve({
      hostname: "127.0.0.1", port: 0,
      fetch(request) {
        const path = new URL(request.url).pathname;
        if (path === "/health") return Response.json({ ready: true, sync_lag_s: 30 });
        if (path === "/index/status") return Response.json({ build_id: buildId });
        return Response.json({ actors: [{ handle: "zzstoatzz.io" }] });
      },
    });
    try {
      for (const prefix of ["b", "compact-b"]) {
        for (const age of [3600, 5 * 24 * 3600]) {
          buildId = `${prefix}${now - age}-21fc-px05`;
          const result = await measureFreshness(fakeEnv({ SEARCH_BACKEND_URL: server.url.origin }));
          expect(result.snapshot_age_s).toBeGreaterThanOrEqual(age);
          expect(result.snapshot_age_s).toBeLessThan(age + 10);
          expect(result.stale).toBe(age > 4 * 24 * 3600);
          expect(result.build_id).toBe(buildId);
        }
      }
    } finally {
      server.stop(true);
    }
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
  const calls: string[] = [];
  const kvWith = (prev: PrevState): FreshnessKv => ({ get: async () => prev });
  const envWith = (prev: PrevState) => fakeEnv({
    SEARCH_BACKEND_URL: "https://backend.test",
    ALERT_WEBHOOK_URL: "https://hook.test/x",
    KV: kvWith(prev),
  });

  const capture = () => {
    calls.length = 0;
    installFetch(async (_u, init) => {
      const payload: { content: string } = JSON.parse(String(init?.body));
      calls.push(payload.content);
      return new Response("{}", { status: 200 });
    });
  };

  const F = (stale: boolean): Freshness => ({
    ok: !stale, stale, replica_lag_s: stale ? 3600 : 60, snapshot_age_s: 7200,
    build_id: "b1-x", probe_ok: true, reasons: stale ? ["replica 3600s behind turso"] : [],
  });

  test("fires when it goes stale", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: false }), F(true));
    expect(calls.length).toBe(1);
    expect(calls[0]).toContain("serving is stale");
  });

  test("fires once on recovery, not on every healthy run", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: true }), F(false));
    expect(calls[0]).toContain("fresh again");
    capture();
    await notifyFreshnessChange(envWith({ stale: false }), F(false));
    expect(calls.length).toBe(0);          // steady healthy -> silent
  });

  test("does not repeat while it stays broken", async () => {
    capture();
    await notifyFreshnessChange(envWith({ stale: true }), F(true));
    expect(calls.length).toBe(0);
  });

  test("silent when no webhook is configured", async () => {
    capture();
    const env = fakeEnv({ SEARCH_BACKEND_URL: "x", KV: kvWith(null) });
    await notifyFreshnessChange(env, F(true));
    expect(calls.length).toBe(0);
  });
});

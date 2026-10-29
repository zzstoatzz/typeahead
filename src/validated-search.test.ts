import { expect, test } from "bun:test";
import { parseRoute } from "./search-route";
import { validatedSearch } from "./validated-search";

const first = parseRoute({ version: 1, current: {
  origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1792908655-14d5-px05",
} });
const second = parseRoute({ version: 1, current: {
  origin: "https://typeahead-search-refresh.fly.dev", build_id: "b1793081565-0228-px05",
} });

test("a changed admission identity discards a same-origin same-build hinted response", async () => {
  const candidate = { engine: "tantivy", origin: "https://typeahead-tantivy-dev.fly.dev", build_id: "b1792908655-test", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "per-segment" } as const;
  const previous = parseRoute({ version: 1, current: candidate });
  const next = parseRoute({ version: 1, current: { ...candidate, admission_sha256: "b".repeat(64) } });
  const arrivals: string[] = [];
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch(request) {
    arrivals.push(new URL(request.url).pathname);
    return new Response(new URL(request.url).pathname, { headers: { "X-Typeahead-Engine": "tantivy", "X-Typeahead-Build": candidate.build_id, "X-Typeahead-Format": candidate.format, "X-Typeahead-Binary-Sha256": candidate.binary_sha256, "X-Typeahead-Retrieval-Strategy": candidate.retrieval_strategy } });
  } });
  try {
    const result = await validatedSearch(Bun.sleep(25).then(() => next), previous, (route, signal) => {
      if (route.engine !== "tantivy") throw new Error("Expected Tantivy fixture route");
      return fetch(new URL('/' + route.admission_sha256, server.url), { signal });
    });
    expect(await result.text()).toBe('/' + 'b'.repeat(64));
    expect(arrivals).toEqual(['/' + 'a'.repeat(64), '/' + 'b'.repeat(64)]);
    expect(result.headers.get("Server-Timing")).toContain('transport_mode;desc="reselected"');
  } finally { server.stop(true); }
});

function fixture(options: { routeDelay: number; originDelay: number; selected?: typeof first; controlStatus?: number; originStatus?: number }) {
  const arrivals: string[] = [];
  const aborted: string[] = [];
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
    const path = new URL(request.url).pathname;
    arrivals.push(path);
    request.signal.addEventListener("abort", () => aborted.push(path), { once: true });
    await Bun.sleep(path === "/control" ? options.routeDelay : options.originDelay);
    if (path === "/control") return Response.json({ version: 1, current: options.selected ?? first }, { status: options.controlStatus ?? 200 });
    return new Response(JSON.stringify(options.originStatus === 503 ? { error: "PdsScopeUnavailable" } : { actors: [{ did: path, handle: "fixture.test", labels: [{ val: "fixture" }] }] }), {
      status: options.originStatus ?? 200, headers: { "X-Typeahead-Pds": "selfhosted.social", "Content-Type": "application/json" },
    });
  } });
  const current = () => fetch(new URL("/control", server.url)).then(async response => {
    if (!response.ok) throw new Error("control unavailable");
    return parseRoute(await response.json());
  });
  const origin = (route: typeof first, signal: AbortSignal) => fetch(new URL("/" + route.build_id, server.url), { signal });
  return { server, arrivals, aborted, current, origin };
}

test("real HTTP control and origin reads overlap while complete responses stay identical", async () => {
  const f = fixture({ routeDelay: 120, originDelay: 160 });
  try {
    let start = performance.now();
    const serial = await validatedSearch(f.current(), undefined, f.origin);
    const serialBody = await serial.text();
    const serialMs = performance.now() - start;
    start = performance.now();
    const overlapped = await validatedSearch(f.current(), first, f.origin);
    const overlapBody = await overlapped.text();
    const overlapMs = performance.now() - start;
    expect(overlapBody).toBe(serialBody);
    expect(overlapped.status).toBe(serial.status);
    expect(overlapped.headers.get("X-Typeahead-Pds")).toBe("selfhosted.social");
    expect(overlapped.headers.get("Server-Timing")).toContain('transport_mode;desc="overlap"');
    expect(overlapMs).toBeLessThan(serialMs - 60);
  } finally { f.server.stop(true); }
});

test("a completed hinted response waits for fresh route validation", async () => {
  const f = fixture({ routeDelay: 120, originDelay: 5 });
  try {
    let returned = false;
    const response = validatedSearch(f.current(), first, f.origin).then(value => { returned = true; return value; });
    await Bun.sleep(40);
    expect(f.arrivals).toContain("/" + first.build_id);
    expect(returned).toBe(false);
    expect((await response).status).toBe(200);
  } finally { f.server.stop(true); }
});

for (const selected of [second, { ...first, build_id: second.build_id }]) {
  test(`route change discards hinted bytes and cancels the old HTTP request: ${selected.origin}`, async () => {
    const f = fixture({ routeDelay: 30, originDelay: 150, selected });
    try {
      const response = await validatedSearch(f.current(), first, f.origin);
      expect(await response.text()).toBe(JSON.stringify({ actors: [{ did: "/" + second.build_id, handle: "fixture.test", labels: [{ val: "fixture" }] }] }));
      expect(response.headers.get("Server-Timing")).toContain('transport_mode;desc="reselected"');
      expect(f.arrivals.filter(path => path !== "/control")).toEqual(["/" + first.build_id, "/" + second.build_id]);
      expect(f.aborted).toContain("/" + first.build_id);
    } finally { f.server.stop(true); }
  });
}

test("a completed response from the previous origin is discarded after validation", async () => {
  const f = fixture({ routeDelay: 80, originDelay: 5, selected: second });
  try {
    const response = await validatedSearch(f.current(), first, f.origin);
    expect(await response.text()).toBe(JSON.stringify({ actors: [{ did: "/" + second.build_id, handle: "fixture.test", labels: [{ val: "fixture" }] }] }));
    expect(response.headers.get("Server-Timing")).toContain('transport_mode;desc="reselected"');
  } finally { f.server.stop(true); }
});

test("route failure fails closed and cancels a slow hinted request", async () => {
  const f = fixture({ routeDelay: 25, originDelay: 500, controlStatus: 503 });
  try {
    const start = performance.now();
    const response = await validatedSearch(f.current(), first, f.origin);
    expect(response.status).toBe(503);
    expect(await response.text()).toBe(JSON.stringify({ error: "SearchRouteUnavailable" }));
    expect(performance.now() - start).toBeLessThan(300);
    await Bun.sleep(10);
    expect(f.aborted).toContain("/" + first.build_id);
  } finally { f.server.stop(true); }
});

test("an HTTP scope error passes through unchanged", async () => {
  const f = fixture({ routeDelay: 10, originDelay: 10, originStatus: 503 });
  try {
    const response = await validatedSearch(f.current(), first, f.origin);
    expect(response.status).toBe(503);
    expect(response.headers.get("X-Typeahead-Pds")).toBe("selfhosted.social");
    expect(await response.text()).toBe(JSON.stringify({ error: "PdsScopeUnavailable" }));
  } finally { f.server.stop(true); }
});

test("a real refused origin connection becomes explicit backend unavailability", async () => {
  const f = fixture({ routeDelay: 10, originDelay: 10 });
  try {
    const response = await validatedSearch(f.current(), first, (_route, signal) => fetch("http://127.0.0.1:1", { signal }));
    expect(response.status).toBe(503);
    expect(await response.text()).toBe(JSON.stringify({ error: "SearchBackendUnavailable" }));
  } finally { f.server.stop(true); }
});

test("an asynchronous hint overlaps real HTTP origin work without bypassing validation", async () => {
  const f = fixture({ routeDelay: 120, originDelay: 5 });
  try {
    let returned = false;
    const hint = fetch(new URL('/hint', f.server.url)).then(() => first);
    const response = validatedSearch(f.current(), hint, f.origin, 'shared').then(value => { returned = true; return value; });
    await Bun.sleep(40);
    expect(f.arrivals).toContain('/' + first.build_id);
    expect(returned).toBe(false);
    expect((await response).status).toBe(200);
  } finally { f.server.stop(true); }
});

test("a slow asynchronous hint cannot hold up an already validated route", async () => {
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  let hintFinished = false;
  const server = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(request) {
    if (new URL(request.url).pathname === '/hint') {
      await gate;
      hintFinished = true;
      return Response.json({ version: 1, current: first });
    }
    return Response.json({ actors: [{ did: 'fixture', handle: 'fixture.test' }] });
  } });
  const hint = fetch(new URL('/hint', server.url)).then(async response => parseRoute(await response.json()));
  try {
    const response = await Promise.race([
      validatedSearch(Promise.resolve(first), hint, (_route, signal) => fetch(new URL('/origin', server.url), { signal }), 'shared'),
      Bun.sleep(1000).then(() => { throw new Error('hint blocked validated route'); }),
    ]);
    expect(await response.text()).toBe(JSON.stringify({ actors: [{ did: 'fixture', handle: 'fixture.test' }] }));
    expect(hintFinished).toBe(false);
  } finally {
    release();
    await hint;
    server.stop(true);
  }
});

import { afterEach, expect, setSystemTime, test } from "bun:test";
import { parseRoute, readRoute, sharedHintsAllowed, type RouteSource } from "./search-route";

const current = { engine: "tantivy", origin: "https://typeahead-slot-a.fly.dev", build_id: "b1792908655-authority", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "c".repeat(64), retrieval_strategy: "global-merge" } as const;
const replacement = { ...current, origin: "https://typeahead-slot-b.fly.dev", build_id: "b1792908655-bucket" } as const;
let clock = 2_200_000_000_000;
afterEach(() => setSystemTime());

function fixture({ stale = false, invalid = false } = {}) {
  setSystemTime(new Date(clock += 10_000));
  const sourceGate = Promise.withResolvers<void>();
  const bucketGate = Promise.withResolvers<void>();
  const bucketStarted = Promise.withResolvers<void>();
  const bucketReceived = Promise.withResolvers<void>();
  const sourceReceived = Promise.withResolvers<void>();
  const background: Promise<unknown>[] = [];
  let closing = false;
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
    const path = new URL(request.url).pathname;
    const source = path === "/source";
    if (source) await sourceGate.promise;
    else { bucketStarted.resolve(); await bucketGate.promise; }
    const route = invalid && path !== "/reset" ? { ...current, build_id: "../../invalid" } : source ? current : replacement;
    return Response.json({ version: 1, current: route }, { headers: {
      "X-Validated-At": String(Date.now() - (stale && source ? 1000 : 0)),
    } });
  } });
  const source: RouteSource = { async current() {
    const response = await fetch(new URL("/source", server.url));
    try {
      return { route: parseRoute(await response.json()), validatedAt: Number(response.headers.get("X-Validated-At")) };
    } finally { sourceReceived.resolve(); }
  } };
  const bucket = { async get(_key: string) {
    const response = await fetch(new URL(closing ? "/reset" : "/bucket", server.url));
    const body = await response.text();
    bucketReceived.resolve();
    const object = new Response(body);
    return { size: body.length, json: object.json.bind(object) };
  } };
  const ctx = { waitUntil(promise: Promise<unknown>) { background.push(promise); } };
  return { source, bucket, ctx, background, sourceReceived: sourceReceived.promise, bucketStarted: bucketStarted.promise, bucketReceived: bucketReceived.promise,
    releaseSource: () => sourceGate.resolve(), releaseBucket: () => bucketGate.resolve(),
    async close() {
      sourceGate.resolve(); bucketGate.resolve();
      await sourceReceived.promise;
      await Promise.allSettled(background);
      setSystemTime();
      closing = true;
      await readRoute(bucket, false);
      await server.stop(true);
    },
  };
}

test("a late fresh authority beats an already-started bucket read without a losing read changing the cache", async () => {
  const f = fixture();
  try {
    let answer: Awaited<ReturnType<typeof readRoute>> | undefined;
    const reading = readRoute(f.bucket, true, f.source, f.ctx).then(value => { answer = value; return value; });
    f.background.push(reading);
    await f.bucketStarted;
    f.releaseSource();
    await f.sourceReceived;
    await Bun.sleep(0);
    expect(answer).toEqual(current);
    f.releaseBucket();
    expect(await reading).toEqual(current);
    await Promise.all(f.background);
    expect(await readRoute(f.bucket, true, f.source, f.ctx)).toEqual(current);
  } finally { await f.close(); }
});

test("a bucket winner is retained when a losing authority response arrives afterward", async () => {
  const f = fixture();
  try {
    const reading = readRoute(f.bucket, true, f.source, f.ctx);
    f.background.push(reading);
    await f.bucketStarted;
    f.releaseBucket();
    expect(await reading).toEqual(replacement);
    f.releaseSource();
    await Promise.all(f.background);
    expect(await readRoute(f.bucket, true, f.source, f.ctx)).toEqual(replacement);
  } finally { await f.close(); }
});

test("a late stale authority cannot win and invalid records still fail closed", async () => {
  const f = fixture({ stale: true });
  try {
    let answer: Awaited<ReturnType<typeof readRoute>> | undefined;
    const reading = readRoute(f.bucket, true, f.source, f.ctx).then(value => { answer = value; return value; });
    f.background.push(reading);
    await f.bucketStarted;
    f.releaseSource();
    await f.sourceReceived;
    await Bun.sleep(0);
    expect(answer).toBeUndefined();
    f.releaseBucket();
    expect(await reading).toEqual(replacement);
    await Promise.all(f.background);
  } finally { await f.close(); }
  const bad = fixture({ invalid: true });
  try {
    bad.releaseSource(); bad.releaseBucket();
    await expect(readRoute(bad.bucket, true, bad.source, bad.ctx)).rejects.toThrow();
    expect(sharedHintsAllowed()).toBe(false);
  } finally { await bad.close(); }
});

test("a failed bucket read fails closed without waiting for a hung authority", async () => {
  const f = fixture({ invalid: true });
  try {
    let failure: unknown;
    const reading = readRoute(f.bucket, true, f.source, f.ctx).catch(error => { failure = error; });
    f.background.push(reading);
    await f.bucketStarted;
    f.releaseBucket();
    await f.bucketReceived;
    await Bun.sleep(0);
    expect(failure).toBeInstanceOf(Error);
    expect(sharedHintsAllowed()).toBe(false);
  } finally { await f.close(); }
});

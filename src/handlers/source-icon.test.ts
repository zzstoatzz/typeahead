import { afterEach, expect, test } from "bun:test";
import type { Stmt, SqlDb } from "../db";
import { handleSourceIcon, iconCandidates } from "./source-icon";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
const realFetch = globalThis.fetch;
const fetched: string[] = [];

// SAFETY: bun has no `caches` global; the handler uses only default.match and
// default.put, which this always-missing stub provides.
const cacheHost = globalThis as { caches?: { default: { match(): Promise<undefined>; put(): Promise<void> } } };
cacheHost.caches = { default: { match: async () => undefined, put: async () => {} } };

afterEach(() => {
  globalThis.fetch = realFetch;
  fetched.length = 0;
});

function serve(responses: Record<string, () => Response>) {
  const fake = async (input: RequestInfo | URL) => {
    const url = String(input);
    fetched.push(url);
    const make = responses[url];
    if (!make) return new Response("nope", { status: 404 });
    const response = make();
    Object.defineProperty(response, "url", { value: url });
    return response;
  };
  // SAFETY: the handler calls fetch(url, init) only; bun's fetch has extra
  // static members (preconnect) the handler never touches.
  globalThis.fetch = fake as typeof fetch;
}

function db(known: string[]): SqlDb {
  return {
    prepare(_sql: string): Stmt {
      let source = "";
      const stmt: Stmt = {
        bind(...args) { source = String(args[0]); return stmt; },
        async all<T>() { const results: T[] = []; return { results }; },
        async first<T>() {
          // SAFETY: the handler's only query selects `1 AS ok`.
          return (known.includes(source) ? { ok: 1 } : null) as T | null;
        },
        async run() { return { meta: { changes: 0 } }; },
      };
      return stmt;
    },
    async batch() { return []; },
  };
}

function ctx(): ExecutionContext {
  const c = { waitUntil: (_p: Promise<unknown>) => {}, passThroughOnException() {} };
  // SAFETY: the handler uses the context only through waitUntil.
  return c as ExecutionContext;
}

const get = (d: string) => new Request(`https://typeahead.waow.tech/source-icon?d=${encodeURIComponent(d)}`);

test("a client name resolves to its brand icon; a host to its favicon", () => {
  expect(iconCandidates("skytrace")).toEqual(["https://skytrace.aly.town/icons/apple-touch-icon.png"]);
  expect(iconCandidates("favs.blue")).toEqual(["https://favs.blue/favicon.ico"]);
  expect(iconCandidates("timeline-ng")).toEqual([]);
  expect(iconCandidates("appview.test")).toEqual([]);
  expect(iconCandidates("10.0.0.1")).toEqual([]);
});

test("an unknown source is never fetched", async () => {
  serve({});
  const res = await handleSourceIcon(get("evil.example"), db([]), ctx());
  expect(res.status).toBe(404);
  expect(fetched).toEqual([]);
});

test("a known source's icon is served from our origin", async () => {
  serve({ "https://favs.blue/favicon.ico": () => new Response(PNG, { headers: { "Content-Type": "image/png" } }) });
  const res = await handleSourceIcon(get("favs.blue"), db(["favs.blue"]), ctx());
  expect(res.status).toBe(200);
  expect(res.headers.get("Content-Type")).toBe("image/png");
  expect(res.headers.get("Content-Security-Policy")).toContain("sandbox");
  expect(new Uint8Array(await res.arrayBuffer())).toEqual(PNG);
});

test("a non-image response is refused", async () => {
  serve({ "https://favs.blue/favicon.ico": () => new Response("<html>", { headers: { "Content-Type": "text/html" } }) });
  const res = await handleSourceIcon(get("favs.blue"), db(["favs.blue"]), ctx());
  expect(res.status).toBe(404);
});

test("a brand icon that fails falls back to the host's favicon", async () => {
  serve({ "https://northsky.app/favicon.ico": () => new Response(PNG, { headers: { "Content-Type": "application/octet-stream" } }) });
  const res = await handleSourceIcon(get("northsky.app"), db([]), ctx());
  expect(fetched).toEqual(["https://northsky.app/static/apple-touch-icon.png", "https://northsky.app/favicon.ico"]);
  expect(res.status).toBe(200);
  expect(res.headers.get("Content-Type")).toBe("image/x-icon");
});

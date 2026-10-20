import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { Miniflare } from "miniflare";

const [backend, unsupportedBackend] = process.argv.slice(2);
for (const url of [backend, unsupportedBackend]) {
  assert.equal(new URL(url).hostname, "127.0.0.1");
}
const directory = await mkdtemp(join(tmpdir(), "typeahead-pds-worker-"));
const bundle = join(directory, "worker.mjs");
const built = spawnSync("bun", ["build", "scripts/pds-worker-entry.js", "--target", "browser", "--outfile", bundle], { stdio: "inherit" });
assert.equal(built.status, 0);
let checks = 0;
async function runtime(bindings, test) {
  const mf = new Miniflare({ modules: true, modulesRoot: directory, scriptPath: bundle, compatibilityDate: "2024-12-01", d1Databases: ["DB"], bindings });
  try {
    const db = await mf.getD1Database("DB");
    await db.exec("CREATE TABLE metrics(hour INTEGER PRIMARY KEY, searches INTEGER DEFAULT 0, total_ms INTEGER DEFAULT 0, cache_hits INTEGER DEFAULT 0, cache_ms INTEGER DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER,PRIMARY KEY(bucket,domain));");
    const fetch = async (query, alias = false) => {
      const path = alias ? "app.bsky.actor.searchActorsTypeahead" : "tech.waow.typeahead.searchActors";
      return mf.dispatchFetch(`https://typeahead.test/xrpc/${path}?${query}`);
    };
    await test(fetch, db);
  } finally {
    await mf.dispose();
  }
}
try {
  await runtime({ SEARCH_BACKEND_URL: backend, TURSO_FALLBACK_ON_EMPTY: "1", TURSO_FALLBACK_ON_UNREACHABLE: "1" }, async (fetch, db) => {
    const global = await (await fetch("q=bob")).text();
    const scoped = await (await fetch("q=bob&pds=acme.test")).text();
    assert.notEqual(scoped, global); checks++;
    assert.equal(await (await fetch("q=bob&pds=acme.test", true)).text(), scoped); checks++;
    assert.equal(await (await fetch("q=bob&pds=HTTPS%3A%2F%2FACME.TEST.%2F")).text(), scoped); checks++;
    assert.equal(await (await fetch("q=bob")).text(), global); checks++;
    assert.equal(await (await fetch("q=bob&pds=sub.acme.test")).text(), '{"actors":[{"did":"did:plc:subdomain","handle":"bob.sub.test","displayName":"Bob","labels":[]}]}'); checks++;
    for (const pds of ["", "*", "a..test", "user@acme.test", "acme.test/path", "acme.test:443"]) {
      assert.equal((await fetch(`q=bob&pds=${encodeURIComponent(pds)}`)).status, 400); checks++;
    }
    assert.equal((await fetch("q=bob&pds=big.test")).status, 503); checks++;
    const empty = await fetch("q=bob&pds=missing.test");
    assert.equal(empty.status, 200);
    assert.deepEqual(await empty.json(), { actors: [] }); checks++;
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok((await db.prepare("SELECT SUM(cache_hits) AS hits FROM metrics").first()).hits >= 1); checks++;
  });
  for (const bindings of [
    { SEARCH_BACKEND_URL: unsupportedBackend },
    { SEARCH_BACKEND_URL: "http://127.0.0.1:1", TURSO_FALLBACK_ON_UNREACHABLE: "1" },
    {},
    { SEARCH_BACKEND_URL: backend, EMERGENCY_PROXY_BSKY: "1" },
  ]) {
    await runtime(bindings, async fetch => {
      const response = await fetch("q=bob&pds=acme.test");
      assert.equal(response.status, 503);
      assert.equal((await response.json()).error, "PdsScopeUnavailable"); checks++;
    });
  }
  console.log(JSON.stringify({ worker_runtime_checks: checks }));
} finally {
  await rm(directory, { recursive: true, force: true });
}

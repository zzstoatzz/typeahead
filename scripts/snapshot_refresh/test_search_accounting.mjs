import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

let healthy = false;
let originCalls = 0;
let polluted = false;
const server = createServer((request, response) => {
  originCalls++;
  setTimeout(() => {
    response.statusCode = healthy ? 200 : 500;
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify(healthy ? { actors: (polluted ? [{ did: 'did:plc:spam', handle: 'donald.trump.com' }, { did: 'did:plc:invalid', handle: 'handle.invalid' }, { did: 'did:plc:fixture', handle: 'fixture.test' }] : [{ did: 'did:plc:fixture', handle: 'fixture.test' }]) } : { error: 'fixture_failure' }));
  }, 25);
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const temporary = mkdtempSync(join(tmpdir(), 'typeahead-accounting-'));
let mf;
try {
  const entry = join(temporary, 'entry.ts');
  const bundle = join(temporary, 'worker.mjs');
  writeFileSync(entry, `import { handleSearch } from ${JSON.stringify(resolve('src/handlers/search.ts'))};
    export default { async fetch(request, env, ctx) {
      const tasks = [];
      const response = await handleSearch(request, env.DB,
        { SEARCH_BACKEND_URL: ${JSON.stringify(origin)}, TURSO_FALLBACK_ON_UNREACHABLE: request.headers.get('X-Fixture-Fallback') ?? undefined },
        { waitUntil(promise) { tasks.push(promise); ctx.waitUntil(promise); } });
      await Promise.all(tasks);
      return response;
    } };`);
  execFileSync('bun', ['build', entry, '--target', 'browser', '--outfile', bundle], { stdio: 'pipe' });
  mf = new Miniflare({ modules: true, script: readFileSync(bundle, 'utf8'), compatibilityDate: '2024-12-01', compatibilityFlags: ['nodejs_compat'], d1Databases: ['DB'],
    outboundService: request => { assert(new URL(request.url).origin === origin); return fetch(request.url, { method: request.method, headers: request.headers, signal: request.signal }); },
  });
  const db = await mf.getD1Database('DB');
  await db.exec('CREATE TABLE metrics(hour INTEGER PRIMARY KEY,searches INTEGER NOT NULL DEFAULT 0,total_ms REAL NOT NULL DEFAULT 0,cache_hits INTEGER NOT NULL DEFAULT 0,cache_ms REAL NOT NULL DEFAULT 0); CREATE TABLE traffic_sources(domain TEXT PRIMARY KEY,hits INTEGER NOT NULL DEFAULT 0); CREATE TABLE traffic_buckets(bucket INTEGER,domain TEXT,hits INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(bucket,domain));');
  const request = (query, headers = {}) => mf.dispatchFetch(`https://fixture.test/xrpc/tech.waow.typeahead.searchActors?${query}`, { headers });
  const metrics = () => db.prepare('SELECT sum(searches) AS misses,sum(cache_hits) AS hits,sum(total_ms) AS total_ms FROM metrics').first();
  assert.deepEqual(await (await request('q=%25')).json(), { actors: [] });
  assert.equal((await metrics()).misses, 1);
  assert.equal(originCalls, 0, 'sanitized-empty answer must not call the backend');
  const emptyTotal = (await metrics()).total_ms;

  const failed = await request('q=fixture');
  assert.equal(failed.status, 200, 'preserve the existing legacy error response contract');
  assert.deepEqual(await failed.json(), { actors: [] });
  assert.equal(originCalls, 1, 'failure must come from the real HTTP origin');
  let counts = await metrics();
  assert.equal(counts.misses, 2);
  assert(counts.total_ms > emptyTotal, 'failed origin wait must remain in the latency total');

  healthy = true;
  const recovered = await request('q=fixture');
  assert.equal((await recovered.json()).actors[0].did, 'did:plc:fixture', 'failed empty answer must not be cached');
  assert.equal((await metrics()).misses, 3);
  assert.equal((await (await request('q=fixture')).json()).actors[0].did, 'did:plc:fixture');
  counts = await metrics();
  assert.equal(counts.misses, 3);
  assert.equal(counts.hits, 1);
  assert.equal(originCalls, 2);

  const scoped = await request('q=scope&pds=selfhosted.social');
  assert.equal(scoped.status, 503);
  assert.equal((await metrics()).misses, 4, 'failed scope acknowledgment is a failed uncached attempt');

  healthy = false;
  assert.deepEqual(await (await request('q=fallback', { 'X-Fixture-Fallback': '1' })).json(), { actors: [] });
  assert.equal((await metrics()).misses, 5, 'backend plus failing SQL fallback counts once');
  polluted = true;
  healthy = true;
  const contained = await request('q=quarantine');
  assert.deepEqual(await contained.json(), { actors: [{ did: 'did:plc:fixture', handle: 'fixture.test' }] });
  const callsBeforeCache = originCalls;
  assert.deepEqual(await (await request('q=quarantine')).json(), { actors: [{ did: 'did:plc:fixture', handle: 'fixture.test' }] });
  assert.equal(originCalls, callsBeforeCache, 'contained cached answers require no extra backend work');
  console.log('PASS: real workerd + Cache API + SQLite/D1 + HTTP origin; sanitized empty, backend error wait, scope failure, SQL fallback failure, recovery and cache hit accounting');
} finally {
  if (mf) await mf.dispose();
  server.closeAllConnections();
  await new Promise(done => server.close(done));
  rmSync(temporary, { recursive: true });
}

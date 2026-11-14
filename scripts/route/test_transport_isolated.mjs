import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:http';

const identity = { engine: 'tantivy', format: 'tantivy-0.27-f32-did-expiry-v4', binary_sha256: 'c'.repeat(64), retrieval_strategy: 'global-merge' };
const a = { ...identity, origin: 'https://typeahead-shadow-a.fly.dev', build_id: 'b1792908655-cp-slota', admission_sha256: 'a'.repeat(64) };
const b = { ...identity, origin: 'https://typeahead-shadow-b.fly.dev', build_id: 'b1793081565-cp-slotb', admission_sha256: 'b'.repeat(64) };
const identify = (response, origin) => {
  const target = [a, b].find(candidate => candidate.origin === origin);
  if (!target) return;
  response.setHeader('X-Typeahead-Engine', 'tantivy');
  response.setHeader('X-Typeahead-Build', target.build_id);
  response.setHeader('X-Typeahead-Format', target.format);
  response.setHeader('X-Typeahead-Binary-Sha256', target.binary_sha256);
  response.setHeader('X-Typeahead-Retrieval-Strategy', target.retrieval_strategy);
};
const arrivals = [];
const server = createServer((request, response) => {
  const url = new URL(request.url, 'http://fixture');
  const origin = request.headers['x-fixture-origin'];
  arrivals.push({ path: url.pathname, method: request.method, origin, routedHealth: request.headers['x-typeahead-routed-health'] });
  const delay = url.searchParams.get('q') === 'slow-shift' && origin === a.origin ? 500 : 10;
  setTimeout(() => {
    response.setHeader('Content-Type', 'application/json');
    identify(response, origin);
    if (url.pathname === '/health') {
      response.end(JSON.stringify({ ready: true, origin }));
    } else {
      if (url.searchParams.has('pds')) response.setHeader('X-Typeahead-Pds', url.searchParams.get('pds'));
      response.end(JSON.stringify({ actors: [{ did: origin, handle: 'fixture.test', labels: [] }] }));
    }
  }, delay);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const address = server.address();
assert(address && typeof address === 'object');
const mf = new Miniflare({
  modules: true, script: readFileSync(process.argv[2], 'utf8'), compatibilityDate: '2024-12-01',
  r2Buckets: ['SEARCH_CONTROL'],
  outboundService: request => {
    const url = new URL(request.url);
    assert([a.origin, b.origin].includes(url.origin), 'unexpected external origin');
    const headers = new Headers(request.headers);
    headers.set('X-Fixture-Origin', url.origin);
    return fetch(`http://127.0.0.1:${address.port}${url.pathname}${url.search}`, {
      method: request.method, headers, body: request.method === 'POST' ? request.body : undefined,
      duplex: 'half', signal: request.signal,
    }).catch(() => Response.json({ error: 'fixture gateway unavailable' }, { status: 502 }));
  },
});
const bucket = await mf.getR2Bucket('SEARCH_CONTROL');
const base = 'https://typeahead-search-transport.n8-3e9.workers.dev';
const put = target => bucket.put('control/search-routing.json', JSON.stringify({ version: 1, current: target }));
const search = (q = 'a') => mf.dispatchFetch(`${base}/search?q=${q}&pds=selfhosted.social`, {
  headers: { 'X-Typeahead-Routed-Health': 'untrusted' },
});
try {
  let response = await search();
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: 'SearchRouteUnavailable' });
  assert.equal(arrivals.length, 0);
  await put(a);
  response = await search();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('X-Typeahead-Pds'), 'selfhosted.social');
  assert.match(response.headers.get('Server-Timing'), /transport_mode;desc="serial"/);
  assert.equal((await response.json()).actors[0].did, a.origin);
  await put(b);
  response = await search();
  assert.equal((await response.json()).actors[0].did, a.origin, 'existing one-second cache window is preserved');
  await new Promise(resolve => setTimeout(resolve, 1100));
  const started = performance.now();
  response = await search('slow-shift');
  assert.equal((await response.json()).actors[0].did, b.origin);
  assert.match(response.headers.get('Server-Timing'), /transport_mode;desc="reselected"/);
  assert(performance.now() - started < 400, 'old origin cancellation delayed the selected response');
  assert(arrivals.filter(row => row.path === '/search').every(row => row.routedHealth === undefined));
  await put(a);
  response = await mf.dispatchFetch(base + '/route');
  assert.deepEqual(await response.json(), a, 'diagnostic routing bypasses the cache');
  response = await mf.dispatchFetch(base + '/admin/refresh', {
    method: 'POST', headers: { Authorization: 'Bearer isolated-invalid-token', 'Content-Type': 'application/json' }, body: '{}',
  });
  assert.equal(response.status, 404);
  assert.deepEqual(arrivals.filter(row => row.method === 'POST'), [], 'writes never reach a backend');
  response = await mf.dispatchFetch(base + '/health');
  assert.equal(response.status, 200);
  assert.equal(arrivals.findLast(row => row.path === '/health').routedHealth, '1');
  await bucket.put('control/search-routing.json', 'not json');
  await new Promise(resolve => setTimeout(resolve, 1100));
  response = await search();
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: 'SearchRouteUnavailable' });
  await new Promise(resolve => setTimeout(resolve, 20));
  const afterFailure = arrivals.length;
  response = await search();
  assert.equal(response.status, 503);
  await response.body.cancel();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(arrivals.length, afterFailure, 'failed route validation disables further speculative origin work');
  await put(b);
  await (await mf.dispatchFetch(base + '/route')).body.cancel();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  response = await search();
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), { error: 'fixture gateway unavailable' });
  assert.equal((await mf.dispatchFetch(base + '/unlisted')).status, 404);
  assert.equal((await mf.dispatchFetch('https://example.invalid/search?q=a')).status, 404);
  const temporary = mkdtempSync(join(tmpdir(), 'typeahead-transport-runtime-'));
  try {
    const entry = join(temporary, 'worker.ts');
    const bundle = join(temporary, 'worker.mjs');
    writeFileSync(entry, `import { validatedSearch } from ${JSON.stringify(resolve('src/validated-search.ts'))};
      const route = ${JSON.stringify(a)} as const;
      export default { fetch() { return validatedSearch(Promise.resolve(route), route,
        (_route, signal) => fetch('http://127.0.0.1:${address.port}/search', { signal })); } };`);
    execFileSync('bun', ['build', entry, '--target', 'browser', '--external', 'cloudflare:workers', '--outfile', bundle], { stdio: 'pipe' });
    const direct = new Miniflare({ modules: true, script: readFileSync(bundle, 'utf8'), compatibilityDate: '2024-12-01' });
    try {
      response = await direct.dispatchFetch('https://fixture.invalid/search');
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { error: 'SearchBackendUnavailable' });
    } finally { await direct.dispose(); }
  } finally { rmSync(temporary, { recursive: true }); }
  console.log('PASS: real workerd + local R2 + real HTTP origins; cache window, route changes, cancellation, errors, PDS acknowledgment, write rejection and health forwarding');
} finally {
  await mf.dispose();
  server.closeAllConnections();
  server.close();
}

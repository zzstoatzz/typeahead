import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:http';

const a = { origin: 'https://typeahead-startup-20261024.fly.dev', build_id: 'b1792908655-14d5-px05' };
const b = { origin: 'https://typeahead-search-refresh.fly.dev', build_id: 'b1793081565-0228-px05' };
const base = 'https://typeahead-search-transport.n8-3e9.workers.dev';
const temporary = mkdtempSync(join(tmpdir(), 'typeahead-shared-hint-'));
const arrivals = [];
let control;
const server = createServer((request, response) => {
  const url = new URL(request.url, 'http://fixture');
  if (url.pathname === '/control') { control = response; return; }
  const origin = request.headers['x-fixture-origin'];
  arrivals.push(origin);
  if (url.searchParams.has('pds')) response.setHeader('X-Typeahead-Pds', url.searchParams.get('pds'));
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({ actors: [{ did: origin, handle: 'fixture.test', labels: [] }] }));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const { port } = server.address();
let mf;
const wait = async condition => {
  const deadline = performance.now() + 2000;
  while (!await condition()) {
    if (performance.now() > deadline) throw new Error('fixture condition timed out');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
};
try {
  const entry = join(temporary, 'worker.ts');
  const bundle = join(temporary, 'worker.mjs');
  writeFileSync(entry, `import worker from ${JSON.stringify(resolve('src/search-transport.ts'))};
    import { parseRoute } from ${JSON.stringify(resolve('src/search-route.ts'))};
    import { HINT_CACHE, HINT_KEY, readSharedHint } from ${JSON.stringify(resolve('src/shared-route-hint.ts'))};
    import { validatedSearch } from ${JSON.stringify(resolve('src/validated-search.ts'))};
    export default { async fetch(request, env, ctx) {
      const path = new URL(request.url).pathname;
      if (path.startsWith('/__fixture/')) {
        const cache = await caches.open(HINT_CACHE);
        if (path === '/__fixture/hint') {
          if (request.method === 'POST') {
            await cache.put(HINT_KEY, new Response(request.body, { headers: { 'Cache-Control': 'public, max-age=60' } }));
            return new Response('stored');
          }
          return await cache.match(HINT_KEY) ?? new Response('missing', { status: 404 });
        }
        if (path === '/__fixture/probe') return Response.json({ hint: await readSharedHint(cache) ?? null });
        if (path === '/__fixture/held-control') {
          const current = fetch('http://127.0.0.1:${port}/control').then(async response => parseRoute(await response.json()));
          return validatedSearch(current, readSharedHint(cache), (route, signal) => fetch(route.origin + '/search', { signal }), 'shared');
        }
      }
      return worker.fetch(request, env, ctx);
    } };`);
  execFileSync('bun', ['build', entry, '--target', 'browser', '--outfile', bundle], { stdio: 'pipe' });
  const start = () => new Miniflare({
    name: 'transport-shared-hint-fixture', modules: true, script: readFileSync(bundle, 'utf8'), compatibilityDate: '2024-12-01',
    r2Buckets: ['SEARCH_CONTROL'], r2Persist: join(temporary, 'r2'), cachePersist: join(temporary, 'cache'),
    outboundService: request => {
      const url = new URL(request.url);
      if (url.origin === 'http://127.0.0.1:' + port && url.pathname === '/control') return fetch(url, { headers: request.headers });
      assert([a.origin, b.origin].includes(url.origin), 'fixture must never reach an external origin');
      const headers = new Headers(request.headers); headers.set('X-Fixture-Origin', url.origin);
      return fetch(`http://127.0.0.1:${port}${url.pathname}${url.search}`, { headers, signal: request.signal });
    },
  });
  const restart = async () => { if (mf) await mf.dispose(); mf = start(); };
  const call = path => mf.dispatchFetch(base + path);
  const probe = async () => (await (await call('/__fixture/probe')).json()).hint;
  const seed = record => mf.dispatchFetch(base + '/__fixture/hint', { method: 'POST', body: typeof record === 'string' ? record : JSON.stringify(record) });
  const record = (route = a, expires_at = Date.now() + 45_000) => ({ route: { version: 1, current: route }, expires_at });
  await restart();
  let bucket = await mf.getR2Bucket('SEARCH_CONTROL');
  await bucket.put('control/search-routing.json', JSON.stringify({ version: 1, current: a }));
  let response = await call('/search?q=a&pds=selfhosted.social');
  assert.equal((await response.json()).actors[0].did, a.origin);
  await wait(async () => (await probe())?.origin === a.origin);
  await restart();
  assert.deepEqual(await probe(), a, 'real Cache API entry must survive a fresh workerd isolate');
  bucket = await mf.getR2Bucket('SEARCH_CONTROL');
  await bucket.put('control/search-routing.json', JSON.stringify({ version: 1, current: b }));
  response = await call('/search?q=a&pds=selfhosted.social');
  assert.equal((await response.json()).actors[0].did, b.origin, 'a persistent hint cannot authorize the previous backend');
  assert.equal(response.headers.get('X-Typeahead-Pds'), 'selfhosted.social');
  for (const invalid of [record(a, Date.now() - 1), record(a, Date.now() + 120_000),
    record(a, Date.now() + .5), record({ ...a, origin: 'https://example.invalid' }), 'not json', 'x'.repeat(16385)]) {
    await (await seed(invalid)).body.cancel();
    assert.equal(await probe(), null, 'expired, unbounded, malformed and unmanaged hints must be ignored');
  }
  await (await seed(record())).body.cancel();
  const before = arrivals.length;
  let returned = false;
  const held = call('/__fixture/held-control').then(value => { returned = true; return value; });
  await wait(() => control && arrivals.length > before);
  assert.equal(arrivals.at(-1), a.origin);
  assert.equal(returned, false, 'completed origin bytes must wait for authoritative validation');
  control.end(JSON.stringify({ version: 1, current: b })); control = undefined;
  response = await held;
  assert.equal((await response.json()).actors[0].did, b.origin);
  assert.match(response.headers.get('Server-Timing'), /transport_mode;desc="reselected"/);
  await (await seed(record())).body.cancel();
  await restart();
  bucket = await mf.getR2Bucket('SEARCH_CONTROL');
  await bucket.delete('control/search-routing.json');
  response = await call('/search?q=a');
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: 'SearchRouteUnavailable' });
  await wait(async () => (await call('/__fixture/hint')).status === 404);
  const afterFailure = arrivals.length;
  response = await call('/search?q=a');
  assert.equal(response.status, 503); await response.body.cancel();
  assert.equal(arrivals.length, afterFailure, 'failed validation must suppress subsequent hinted fetches');
  await bucket.put('control/search-routing.json', JSON.stringify({ version: 1, current: b }));
  response = await call('/search?q=a');
  assert.equal((await response.json()).actors[0].did, b.origin);
  await wait(async () => (await probe())?.origin === b.origin);
  console.log('PASS: real persistent Cache API across workerd restart; bounded/expired/malformed hints, exact routing validation, PDS acknowledgment, cancellation and R2 failure/recovery');
} finally {
  if (mf) await mf.dispose();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  rmSync(temporary, { recursive: true });
}

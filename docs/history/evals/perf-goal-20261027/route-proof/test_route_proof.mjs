import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const a = { origin: 'https://typeahead-startup-20261024.fly.dev', build_id: 'b1792908655-14d5-px05' };
const b = { origin: 'https://typeahead-search-refresh.fly.dev', build_id: 'b1793081565-0228-px05' };
const pending = [];
const server = createServer((request, response) => {
  if (request.url === '/gate') { pending.push(response); return; }
  response.setHeader('Content-Type', 'application/json');
  response.setHeader('X-Typeahead-Pds', 'selfhosted.social');
  response.end(JSON.stringify({ actors: [{ did: request.headers['x-origin'], handle: 'fixture.test' }] }));
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const { port } = server.address();
const temporary = mkdtempSync(join(tmpdir(), 'typeahead-route-proof-'));
let mf;
const release = () => { for (const response of pending.splice(0)) response.end('ready'); };
const sleep = ms => new Promise(done => setTimeout(done, ms));
try {
  const entry = join(temporary, 'worker.ts');
  const bundle = join(temporary, 'worker.mjs');
  writeFileSync(entry, `import worker from ${JSON.stringify(resolve('src/search-transport.ts'))};
    import { readRoute, routeHintRecord, parseRoute } from ${JSON.stringify(resolve('src/search-route.ts'))};
    import { HINT_CACHE, HINT_KEY } from ${JSON.stringify(resolve('src/shared-route-hint.ts'))};
    export default { async fetch(request, env, ctx) {
      const url = new URL(request.url);
      if (url.pathname === '/__fixture/prime') {
        await readRoute(env.REAL_CONTROL, false);
        return Response.json(routeHintRecord());
      }
      if (url.pathname === '/__fixture/producer') {
        const object = await env.REAL_CONTROL.get('control/search-routing.json');
        const route = parseRoute(await object.json());
        const now = Date.now();
        const record = { route: { version: 1, current: route }, expires_at: now + 45_000,
          verified_until: now + (url.searchParams.has('future') ? 30_000 : 1000) };
        const cache = await caches.open(HINT_CACHE);
        await cache.put(HINT_KEY, Response.json(record, { headers: { 'Cache-Control': 'public, max-age=60' } }));
        return Response.json(record);
      }
      if (url.pathname === '/__fixture/record') {
        return await (await caches.open(HINT_CACHE)).match(HINT_KEY) ?? new Response('missing', { status: 404 });
      }
      const gated = { get: async (...args) => {
        await fetch('http://127.0.0.1:${port}/gate');
        return env.REAL_CONTROL.get(...args);
      } };
      return worker.fetch(request, { SEARCH_CONTROL: gated }, ctx);
    } };`);
  execFileSync('bun', ['build', entry, '--target', 'browser', '--outfile', bundle], { stdio: 'pipe' });
  mf = new Miniflare({ modules: true, script: readFileSync(bundle, 'utf8'), compatibilityDate: '2024-12-01', r2Buckets: ['REAL_CONTROL'],
    outboundService: request => {
      const url = new URL(request.url);
      if (url.origin === `http://127.0.0.1:${port}` && url.pathname === '/gate') return fetch(url);
      assert([a.origin, b.origin].includes(url.origin));
      return fetch(`http://127.0.0.1:${port}/origin`, { headers: { 'X-Origin': url.origin }, signal: request.signal });
    },
  });
  const bucket = await mf.getR2Bucket('REAL_CONTROL');
  const base = 'https://typeahead-search-transport.n8-3e9.workers.dev';
  const call = path => mf.dispatchFetch(base + path);
  const put = route => bucket.put('control/search-routing.json', JSON.stringify({ version: 1, current: route }));
  const prime = async () => { await put(a); await (await call('/__fixture/prime')).body.cancel(); await sleep(1100); };
  const search = () => call('/search?q=a&pds=selfhosted.social');
  await prime();
  const proof = await (await call('/__fixture/producer')).json();
  await put(b);
  const response = await Promise.race([search(), sleep(250).then(() => { throw new Error('fresh proof did not release known-route response'); })]);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('X-Typeahead-Pds'), 'selfhosted.social');
  assert.equal((await response.json()).actors[0].did, a.origin);
  assert(pending.length > 0, 'authoritative R2 read must remain independently pending');
  assert.equal((await (await call('/__fixture/record')).json()).verified_until, proof.verified_until, 'borrowing a proof must not renew its lifetime');
  await sleep(Math.max(0, proof.verified_until - Date.now()) + 30);
  let returned = false;
  const expired = search().then(value => { returned = true; return value; });
  await sleep(50);
  assert.equal(returned, false, 'the long-lived hint cannot authorize after the original proof expires');
  release();
  assert.equal((await (await expired).json()).actors[0].did, b.origin);

  for (const [target, suffix] of [[b, ''], [{ ...a, build_id: b.build_id }, ''], [a, '?future=1']]) {
    await prime();
    await put(target);
    await (await call('/__fixture/producer' + suffix)).body.cancel();
    let returned = false;
    const result = search().then(value => { returned = true; return value; });
    await sleep(50);
    assert.equal(returned, false, 'an unknown route/build or oversized proof lifetime cannot replace current validation');
    release();
    const response = await result;
    assert.equal(response.status, 200);
    assert.equal((await response.json()).actors[0].did, target.origin);
  }
  console.log('PASS: real R2 + Cache API + held HTTP authority; known-route proof reuse, original expiry, no renewal, unknown origin/build and future-proof rejection');
} finally {
  release();
  if (mf) await mf.dispose();
  server.closeAllConnections();
  await new Promise(done => server.close(done));
  rmSync(temporary, { recursive: true });
}

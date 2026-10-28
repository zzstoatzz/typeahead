import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
let mode = 'dns';
const did = 'did:plc:verification-test';
const handle = 'identity.test';
const server = createServer((request, response) => {
  const host = request.headers['x-fixture-host'];
  response.setHeader('Content-Type', 'application/json');
  if (host === 'plc.directory') response.end(JSON.stringify({ id: did, alsoKnownAs: mode === 'missing-claim' ? [] : [`at://${handle}`] }));
  else if (host === 'cloudflare-dns.com') response.end(JSON.stringify({ Status: 0, Answer: mode === 'https' ? [] : [{ type: 16, data: `"did=${mode === 'spoof' ? 'did:plc:other' : did}"` }] }));
  else if (host === handle) response.end(did);
  else { response.statusCode = 500; response.end('{}'); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const dir = mkdtempSync(join(tmpdir(), 'typeahead-identity-verification-'));
let runtime;
try {
  writeFileSync(join(dir, 'entry.ts'), `import { verifyIdentityHandle } from ${JSON.stringify(resolve('src/verify-identity.ts'))}; export default { async fetch() { return Response.json({ verified: await verifyIdentityHandle(${JSON.stringify({ did, handle, pds: 'https://pds.identity.test' })}) }); } };`);
  execFileSync('bun', ['build', join(dir, 'entry.ts'), '--target', 'browser', '--outfile', join(dir, 'worker.mjs')]);
  runtime = new Miniflare({ modules: true, script: readFileSync(join(dir, 'worker.mjs'), 'utf8'), compatibilityDate: '2024-12-01', outboundService: request => {
    const url = new URL(request.url);
    assert(['plc.directory', 'cloudflare-dns.com', handle].includes(url.hostname));
    return fetch(origin + url.pathname + url.search, { headers: { 'X-Fixture-Host': url.hostname } });
  }});
  for (const [value, expected] of [['dns', true], ['https', true], ['spoof', false], ['missing-claim', false]]) {
    mode = value;
    assert.equal((await (await runtime.dispatchFetch('http://fixture.test/')).json()).verified, expected, value);
  }
  console.log('PASS: real workerd and HTTP; forward claim, DNS ownership, HTTPS fallback, spoof rejection');
} finally {
  if (runtime) await runtime.dispose();
  server.closeAllConnections();
  await new Promise(done => server.close(done));
  rmSync(dir, { recursive: true });
}

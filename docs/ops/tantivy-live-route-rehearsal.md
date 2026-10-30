# Isolated routing rehearsal with live backends

The optional mode in `scripts/snapshot_refresh/route-rehearsal.test.ts` runs the
existing public search handler, transport, cache and conditional route updater in
Miniflare. Its R2 routing object and metrics database are temporary local files.
It never reads or writes the production routing object and deploys no Worker.
The normal test run makes no remote requests. Live execution requires explicit
approval that the candidate is ready.

Create a small JSON file from the current native route identity and the prepared
candidate plan. Use their actual values; no credential belongs in this file:

```json
{
  "schema_version": 1,
  "native": {
    "origin": "https://typeahead-startup-20261024.fly.dev",
    "build_id": "<current native build>"
  },
  "candidate": {
    "engine": "tantivy",
    "origin": "https://typeahead-tantivy-a.fly.dev",
    "build_id": "<prepared candidate build>",
    "format": "tantivy-0.27-f32-did-expiry-v4",
    "admission_sha256": "<prepared admission SHA256>",
    "binary_sha256": "<installed binary SHA256>",
    "retrieval_strategy": "global-merge"
  }
}
```

After candidate readiness is independently verified, run from the Typeahead root:

```sh
TYPEAHEAD_REHEARSAL_LIVE=1 \
TYPEAHEAD_REHEARSAL_LIVE_CONFIG=/absolute/path/live-route.json \
TYPEAHEAD_REHEARSAL_LIVE_EVIDENCE=/absolute/path/new-live-route-evidence.json \
bun test scripts/snapshot_refresh/route-rehearsal.test.ts -t 'approved live backends'
```

The evidence file must be new. The owning route parser validates the input, and
live mode permits only the two approved native origins and parallel Tantivy A/B.
The adapter forwards `GET /search?q=zzstoatzz.io&limit=10` plus fixed read-only
`/health` and `/index/status` probes on the selected native origin. It strips credentials,
rejects redirects, limits responses to 1 MiB, starts requests at least one second
apart, and stops at sixteen attempts or a sixty-second network budget. Each
upstream request has a two-second timeout. Unexpected egress is rejected before
network access.

The sequence verifies both public aliases, switches the local R2 object to the
candidate, rejects a stale ETag, then rolls that local object back to native.
Search responses must identify the expected engine/build and the candidate's
actual binary/strategy; the known handle must be first. Native search omits a
build header on existing deployments, so native identity is established by
health/index probes before and after each native phase. These must report the
selected build, an attached FLATPX05 snapshot and unchanged snapshot watermark.
An absent native search build header stays null in evidence; any present header
must match. No identity header is fabricated. Alias bodies must agree.
Each phase uses a separate local cache generation after a 1.1-second local route
settling interval. This is functional evidence, not a propagation or latency
measurement. The report retains body hashes and counts, not actor payloads.

This does not qualify live source coverage, provider resources, performance,
artifact refresh or production cutover. Those checks remain independent, and the
report explicitly leaves release qualification false. Temporary local R2/cache
files and workerd processes are disposed on success or failure.

Offline verification of this same execution path uses actual loopback HTTP
servers. It covers native/candidate/rollback routing without contacting Fly,
plus request rejection, credential stripping, redirect refusal and bounded body
reads:

```sh
bun test scripts/snapshot_refresh/rehearsal-live-origin.test.ts scripts/snapshot_refresh/route-rehearsal.test.ts
```

The October 30 [real-origin receipt](../evals/tantivy-dev-20261028/checkpoint-live-route-rehearsal.json)
passes native→A→native using the installed checkpoint candidate. It preserves
the earlier [failed header-assumption receipt](../evals/tantivy-dev-20261028/checkpoint-live-route-rehearsal-failed-native-header.json).
Native searches omit their build header; the corrected proof uses their actual
health/index contract. All route mutations in this run were local R2 operations;
production routing and serving machines were unchanged.

# Isolated routing rehearsal with live backends

`just rehearse-route-live` (`scripts/snapshot_refresh/route-rehearsal-live.ts`)
runs the existing public search handler, transport, cache and conditional route
updater in Miniflare against the two live Tantivy slots. Its R2 routing object
and metrics database are temporary local files. It never reads or writes the
production routing object and deploys no Worker. Live execution requires
explicit approval that the candidate is ready.

Create a small JSON file from the currently routed slot and the prepared
candidate plan. Use their actual values; no credential belongs in this file:

```json
{
  "schema_version": 2,
  "previous": {
    "engine": "tantivy",
    "origin": "https://typeahead-tantivy-a.fly.dev",
    "build_id": "<routed build>",
    "format": "tantivy-0.27-f32-did-expiry-v4",
    "admission_sha256": "<routed admission SHA256>",
    "binary_sha256": "<routed binary SHA256>",
    "retrieval_strategy": "global-merge"
  },
  "candidate": {
    "engine": "tantivy",
    "origin": "https://typeahead-tantivy-b.fly.dev",
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
just rehearse-route-live /absolute/path/live-route.json /absolute/path/new-live-route-evidence.json
```

The evidence file must be new. The owning route parser validates both targets,
and live mode permits only Tantivy A and B, one on each side. The adapter
forwards only `GET /search?q=zzstoatzz.io&limit=10`. It strips credentials,
rejects redirects, limits responses to 1 MiB, starts requests at least one second
apart, and stops at sixteen attempts or a sixty-second network budget. Each
upstream request has a two-second timeout. Unexpected egress is rejected before
network access.

The sequence verifies both public aliases on the routed slot, switches the local
R2 object to the candidate, rejects a stale ETag, then rolls that local object
back. Search responses must identify the expected engine, build, binary and
retrieval strategy; the known handle must be first, and alias bodies must agree.
Each phase uses a separate local cache generation after a 1.1-second local route
settling interval. This is functional evidence, not a propagation or latency
measurement. The report retains body hashes and counts, not actor payloads.

This does not qualify live source coverage, provider resources, performance,
artifact refresh or production cutover. Those checks remain independent, and the
report explicitly leaves release qualification false. Temporary local R2/cache
files and workerd processes are disposed on success or failure.

Offline verification of this same execution path uses actual loopback HTTP
servers, plus request rejection, credential stripping, redirect refusal and
bounded body reads:

```sh
bun test scripts/snapshot_refresh/rehearsal-live-origin.test.ts scripts/snapshot_refresh/route-rehearsal.test.ts
```

The October 30 [real-origin receipt](../evals/tantivy-dev-20261028/checkpoint-live-route-rehearsal.json)
is historical: it rehearsed the retired native engine against slot A with the
earlier schema-version-1 configuration.

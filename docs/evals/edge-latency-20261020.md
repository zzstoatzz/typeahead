# Cold edge latency, October 20, 2026

Live at 16:31 UTC: public Worker `89901546-0e01-4789-b19a-541f0545460e`
uses the private `typeahead-search-transport` service binding for search cache
misses. Transport version `786da370-0cc2-499b-9238-25e25de81267` has explicit
`aws:us-east-1` placement. It reached Fly ingress IAD in every qualification and
public verification request. Rate limiting, cache lookup and cache hits remain
in the public edge Worker. The transport has no public route or preview URL.

## Diagnosis

Logfire's initial 43 sampled compact search spans had a 2.66 ms median,
7.08 ms p95 and 7.80 ms maximum. These start inside the engine and omit transport
and pre-handler waiting. Worker response timing then reproduced an origin hop
of 1,828 ms, versus 29 ms checking cache. Atlanta requests reached Fly through
GRU (São Paulo) and SYD (Sydney), despite the backend running in EWR (Newark).

An IPv4-only DNS alias did not eliminate overseas ingress. Its 20-request median
improved only from 375 to 366 ms, with four GRU requests. That experiment was
rejected. The original `.fly.dev` origin is serving again; the experimental A,
TXT and Fly certificate were deleted and DNS-record absence verified.
Cloudflare's `cf` CLI had working OAuth DNS permissions; Wrangler's separate
credentials did not. No user DNS changes were needed.

## Live measurements

The same 20 paced global/scoped queries from HeavyPad, limit 8, all confirmed
cache misses and HTTP 200. All 20 response bodies matched before/after.
Each curl request establishes a new connection, unlike the pooled benchmark.
Percentiles below use nearest rank; these are small bounded probes, not a load test.

| Measurement | Before | Placed transport |
| --- | ---: | ---: |
| Public mean | 497 ms | 397 ms |
| Public median | 375 ms | 378 ms |
| Public p95 | 1,188 ms | 688 ms |
| Public maximum | 1,274 ms | 725 ms |
| Origin-hop median | 120 ms | 113 ms |
| Origin-hop p95 | 824 ms | 178 ms |
| Fly ingress | 17 IAD, 3 GRU | 20 IAD |

This demonstrates a 20% mean and 42% p95 improvement in this campaign; its
median was unchanged. The separate transport qualification also returned 20
byte-identical responses, all through IAD.

The standard `just bench --quick --no-stress` run at 16:33 UTC uses a pooled
HTTP client, 10 queries and one cold/warm trial each:

| Benchmark | Earlier 15:22 release run | 16:33 placed transport |
| --- | ---: | ---: |
| Cold p50 | 675 ms | 143 ms |
| Cold p95 | 2,118 ms | 331 ms |
| Warm p50 | 84 ms | 57 ms |
| Warm p95 | 121 ms | 79 ms |

These runs were not interleaved, so do not attribute every difference solely
to placement. The closer paired campaign above independently confirms the tail
improvement. Bluesky cold p50/p95 in the new run was 102/227 ms; we do not claim
to beat it on all queries. Mean overlap stayed 45%; ranking was not changed.

## Correctness and rollback

- Full authenticated live smoke passed from HeavyPad.
- 35 PDS/alias acceptance responses passed, including normalized scopes,
  global/scoped cache isolation, invalid inputs, unknown hosts and an excluded
  large host failing explicitly.
- Transport accepts only GET `/search` on the two known Fly origins. Admin paths,
  arbitrary origins, plaintext HTTP and mutations are rejected. It adds no
  database queries, retry, cache, candidate processing or ranking work.
- Both calls retain a three-second timeout; existing scoped acknowledgment and
  fail-closed behavior remain in the public handler.
- Post-cutover Logfire: 12 sampled compact searches, median 2.38 ms, p95 4.82 ms,
  maximum 5.56 ms; latest 16:34:49 UTC. No error-level records appeared in that
  inspected window. Sampling is not a claim of complete error coverage.
- The previously verified loading-state cleanup remains live: no transient
  “Searching…” dropdown, stale query/scope results cleared, `aria-busy` set while
  waiting, filter closed by default.

Immediate transport rollback is public Worker
`2c4615e4-1500-40b4-9ec0-4fedd5e8540b`, which preserves PDS filtering and the
loading fix but calls the original compact origin directly. Refresh deployment
state first and preserve any newer unrelated source. Alternatively remove the
`SEARCH_TRANSPORT` binding from current configuration and deploy current source;
the handler's unbound path uses the existing direct fetch. Never roll back to
experimental version `1496f156-41b9-4674-84de-a8e437064b9a`, which used the deleted
DNS alias. Native images, machines, snapshots and recovery pairs were untouched.
Legacy backend rollback still requires disabling scoped search first.

Deploy the transport before the public Worker on a new account. Both configurations
are built in CI; `just deploy-search-transport` deploys it and runs public smoke.
Placement configuration follows the [Cloudflare placement documentation](https://developers.cloudflare.com/workers/configuration/placement/).

Sanitized timings are alongside this document. Full response bodies, Logfire,
smoke and benchmark evidence and the reproducible paced probe script remain at
`~/.local/share/typeahead-experiments/edge-latency-20261020/`.

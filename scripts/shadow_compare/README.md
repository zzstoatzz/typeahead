# shadow comparison

decides whether a shadow search backend may take production traffic. it compares the shadow with production on corpus, search results, latency and freshness, and writes a PASS or FAIL per gate. it is the comparison job in step 2 of "proving it before it serves" in the [embedded-store plan](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md), tracked in issue 43.

status, 2026-11-10: tested against local fixtures and two local Tantivy servers only. no shadow exists yet and it has not been run against production's admin listener.

both backends are the [Tantivy server](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md). the tool only reads: `/search`, `/debug/search`, `/ready` and `/index/status` on the public listener, `/admin/source-page` and `/admin/source-state` on the admin listener.

## what each phase proves

**corpus** walks both identity sets in DID order and reads every DID from both backends. a DID is live on a backend when a document exists and is not a tombstone. each differing DID falls in one or more classes: `only_in_production`, `only_in_shadow`, `handle_differs`, `visibility_differs` (the stored `hidden` flag), `pds_differs` (the canonical PDS host), `other_field_differs` (raw PDS value, quality score, or the hash of display name, avatar, labels, associated and creation time). differing DIDs are read once more after a settle delay and only those still differing are counted. each backend's build and watermark are recorded at the start and end, so the report states the window the comparison covers. with `--arbiter`, a deterministic sample per class (the DIDs with the smallest SHA-256) is checked against the DID's own authority and labelled `shadow is right`, `production is right`, `both stale` or `undetermined`.

it does not prove agreement on anything source-state does not return. the source version and the follower, follow, post and activity counters are ignored on purpose: they do not affect ranking or the response. operator overrides and blocked domains live in the moderation policy, not in actor documents, so they are compared only as a policy hash in the freshness phase. the arbiter covers a sample, not every difference; it trusts the appview's handle verification rather than doing its own DNS and well-known checks; it cannot judge visibility, quality score or profile content, which stay `undetermined` and fail the gate.

a server that predates the `fields` option of source-state returns only a document hash. that hash includes the source version, so against such a server every DID two independent backends hold shows as `document_hash_differs` and the corpus gate cannot pass.

**search** runs the canonical sets from [bench.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/bench.py), the [recorded candidate and PDS-scope cases](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/tantivy-dev-ui/candidate-smoke-cases.json), and any extra queries, against both backends directly. per query it reports identical ordered DIDs or the first differing rank, set overlap, actors whose response body differs, whether the scope acknowledgment headers agree, and whether bench.py's structural checks and each case's recorded expectations pass on each side. a differing query is run once more after a settle delay. a remaining difference is explained only when a DID the corpus phase flagged appears in either result.

it does not prove agreement on queries outside the set, and an explanation is an association, not a derivation: a flagged DID in the results does not show that it caused the difference. bench.py's spam guard needs Bluesky's answer and runs only with `--bluesky-oracle`. the public Worker path can be compared with `--production-public`, for information only, since its cache may be a minute old.

**latency** sends the same queries one at a time, alternating which backend goes first, once cold and several times warm. it records the backend's own `Server-Timing` search duration and the client-observed time, p50, p95 and p99 for each, and a paired per-query difference. the gate uses server time when every response carried it, otherwise client time. the report lists what was controlled and what was not; client time includes the network path from wherever the tool runs, so it compares the backends only when both are reached from the same region through the same kind of hop. nothing here measures behaviour under concurrent load.

**freshness** samples `/ready` and `/index/status` on both backends over a window: sync lag, watermark movement, moderation freshness, and the normalizer, plan, format and moderation hashes that must agree for results to agree. given a file of recently changed DIDs it also reads them from both admin listeners each sample and reports when the backends first agreed on each.

## load on production

every target has its own request and byte budget and a fixed delay between requests; the tool stops when either budget is spent. nothing runs concurrently. the defaults allow 2,000 admin requests and 256 MiB per backend, which is a partial walk: a full walk has to be asked for with larger budgets, and resumes from `--checkpoint` when it stops. search, latency and freshness share 2,000 requests per backend at one request a second.

source-page and source-state each take at most 500 DIDs. a window of 500 DIDs costs one page and one state request per backend. measured on a local server with production-shaped records, that is 17.6 kB for the page and 230 kB for the state request and response with fields (82 kB without). each state request hydrates up to 500 stored documents on the serving machine, which is the real cost to production and the reason for the delay.

the arbiter makes one request per sampled DID to its authority and one per 25 DIDs to the appview, a second apart by default.

## inputs

`--production` and `--shadow` are the backends' public origins. `--production-admin` and `--shadow-admin` are their admin listeners, which bind loopback on the machine, so the corpus phase and `--changed-dids` must run where both are reachable. `--production-admin-secret-env` and `--shadow-admin-secret-env` name the environment variables that hold each admin bearer. the values are never printed or written to the report, are sent only to `/admin/` paths over https or loopback, and are redacted from error text.

`--phases` selects among `corpus`, `search`, `latency` and `freshness`. `--extra-queries` is a file with one query, or one JSON object of search parameters, per line. `--changed-dids` is a file with one DID per line. `--flagged-from` reads the flagged DIDs from an earlier report so search can run without repeating the walk. `--out-dir` is where the report goes; the default is a dated directory under [docs/evals](https://tangled.org/zzstoatzz.io/typeahead/tree/main/docs/evals).

## gates

| gate | passes when |
|---|---|
| (a) corpus | the walk completed and differing DIDs were re-read; every class has zero differences, or every sampled difference in it is labelled `shadow is right` or `both stale` |
| (b) search | no check fails on the shadow, and every query whose results differ contains a flagged DID |
| (c) latency | for cold and warm, shadow p50, p95 and p99 are each at most production's value × 1.10 + 2 ms |
| (d) freshness | both backends ready, sync lag at most 120 s and moderation fresh at every sample, and each must-match value equal in at least one sample |

| constant | value | meaning |
|---|---|---|
| `ARBITER_SAMPLE_SIZE` | 25 | DIDs sampled per difference class |
| `ARBITER_PASSING_LABELS` | `shadow is right`, `both stale` | labels that do not fail gate (a) |
| `SETTLE_DELAY_S` | 120 | wait before differing DIDs are re-read |
| `MAX_TRACKED_DIFFERENCES` | 50,000 | differing DIDs kept for the re-read; any beyond it fail gate (a) |
| `LATENCY_NOISE_RATIO` | 0.10 | relative allowance in gate (c) |
| `LATENCY_NOISE_FLOOR_MS` | 2.0 | absolute allowance in gate (c) |
| `LATENCY_MIN_QUERIES` | 20 | fewer measured queries fail gate (c) |
| `FRESH_MAX_SYNC_LAG_S` | 120 | the cutover polling bound the server already reports |
| `FRESH_MIN_SAMPLES` | 3 | fewer samples fail gate (d) |

the latency allowance is a stated bound, not a measured noise level; no run against real backends has calibrated it. `both stale` passes because the bar is the shadow being no worse than production. a gate whose phase did not run is `NOT_RUN` and the overall verdict is then `INCOMPLETE`. the exit status is 1 when any gate fails, 2 when the run could not finish, 0 otherwise.

## run

```sh
uv run scripts/shadow_compare/compare.py \
  --production https://typeahead-tantivy-a.fly.dev --shadow https://SHADOW \
  --phases search,latency,freshness
```

the corpus phase adds the admin listeners, the two variable names and a budget sized for the walk:

```sh
uv run scripts/shadow_compare/compare.py \
  --production http://127.0.0.1:8080 --production-admin http://127.0.0.1:8081 \
  --shadow http://127.0.0.1:9080 --shadow-admin http://127.0.0.1:9081 \
  --production-admin-secret-env PRODUCTION_ADMIN_SECRET --shadow-admin-secret-env SHADOW_ADMIN_SECRET \
  --phases corpus,search,latency,freshness --arbiter \
  --checkpoint walk.json --corpus-max-requests 60000 --corpus-max-mib 8192
```

## develop

```sh
just --no-dotenv test-shadow-compare   # builds typeahead-tantivy, runs unit tests and the two-server integration test
```

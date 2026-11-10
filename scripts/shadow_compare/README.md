# shadow comparison

decides whether a shadow search backend may take production traffic. it compares the shadow with production on corpus, search results, latency and freshness, and writes a PASS or FAIL per gate. it is the comparison job in step 2 of "proving it before it serves" in the [embedded-store plan](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md), tracked in issue 43.

status, 2026-11-10: tested against local fixtures, local Tantivy servers, an index read back through `dump-actors`, and a fake of the store's read endpoints. no shadow exists yet, the store process does not serve those endpoints yet, and nothing here has run against production or a real bundle.

both backends are the [Tantivy server](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md). the tool only reads: `/search`, `/debug/search`, `/ready` and `/index/status` on the public listener, `/admin/source-page` and `/admin/source-state` on the admin listener, and the two store read endpoints described under corpus sources.

## what each phase proves

**corpus** walks both sides' actors in DID order and compares every DID. a DID is live on a side when a document exists and is not a tombstone. each differing DID falls in one or more classes: `only_in_production`, `only_in_shadow`, `handle_differs`, `visibility_differs` (the stored `hidden` flag), `pds_differs` (the canonical PDS host), `labels_differ`, `quality_score_differs` (compared at single precision) and `other_field_differs` (raw PDS value, display name, avatar, associated, creation time). when both sides hold whole actors the report names the labels held by one side only, counts them by label value, and names which profile field differs; an avatar stored as a CID on one side and a URL to the same CID on the other is `avatar_form`. when a side is an admin listener only the server's profile hash is available, so a label or profile difference shows as `profile_sha256` under `other_field_differs`. every class is also counted by handle kind (under `.bsky.social`, custom domain, none), by PDS kind (Bluesky-run, independent, none) and by its ten most frequent PDS hosts. differing DIDs are read once more after a settle delay and only those still differing are counted. each backend's build and watermark are recorded at the start and end, so the report states the window the comparison covers. with `--arbiter`, a deterministic sample per class (the DIDs with the smallest SHA-256) is checked against the DID's own authority and labelled `shadow is right`, `production is right`, `both stale` or `undetermined`.

it does not prove agreement on anything source-state does not return. the source version and the follower, follow, post and activity counters are ignored on purpose: they do not affect ranking or the response. operator overrides and blocked domains live in the moderation policy, not in actor documents, so they are compared only as a policy hash in the freshness phase. the arbiter covers a sample, not every difference; it trusts the appview's handle verification rather than doing its own DNS and well-known checks; it cannot judge visibility, labels, quality score or profile content, which stay `undetermined`.

## corpus sources

production's admin listener binds loopback and its binary predates the `fields` option, so production is normally read from its published bundle instead. `--production-bundle` takes a view file made from [dump-actors](https://tangled.org/zzstoatzz.io/typeahead/blob/main/experiments/zkv-scale/README.md) output, which prints every stored actor of a bundle's index, tombstones included:

```sh
dump-actors INDEX_DIR | uv run scripts/shadow_compare/bundle.py sort --input - --out views.jsonl --tmp SORT_DIR
```

the dump is in segment order. the sort reduces each line to its comparable view, writes sorted runs of 250,000 views and merges them, so memory holds one run and `SORT_DIR` plus the output hold the views twice at the peak. measured on a laptop with 500,000 synthetic actors (262 MB): 8 s, 374 MB of memory, and 603 MB of disk at the peak, of which 298 MB is the output that stays. scaled linearly to the 13.2 million actors and 6.7 GB of the 2026-11-10 bundle that is about 15 GB of disk at the peak and under four minutes on the same laptop, with memory unchanged because the run size is fixed; no run at that size has been made and a cloud builder's cores are slower. the index refuses to serve two live documents for one DID, so duplicates are not expected; if any appear the highest version wins, then the later line, and the report states the count.

a bundle is production at its watermark, not now. with `--bundle-watermark`, each differing DID is also classed by the shadow record's own change time: changed after the watermark less 120 s is `newer_in_shadow`, anything else, including a DID the shadow lacks or a shadow that reports no change time, is `unchanged_in_shadow` and is divergence. the report gives both partitions with their own class tables.

`--shadow-store` reads the shadow's store through the store process instead of the shadow's admin listener. it needs two POST endpoints under `/store/`, a page of records after a DID and a batch of records by DID, with a bearer secret named by `--shadow-store-secret-env`; the request and response shapes are stated at the top of [store.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/store.py). a record is compared as the document the index bridge would send for it, and its `updated_at` is the change time. the shadow's admin listener still works as the shadow side, but its documents carry a change-log sequence, not a time, so against a bundle every difference then counts as divergence.

what this cannot detect: a difference in a field neither side stores in the actor (overrides and blocked domains); a store whose `updated_at` does not track content changes, beyond the one-quarter bound in gate (a); a shadow record that changed after the watermark for an unrelated reason while also being wrong about something older, which lands in `newer_in_shadow` and is caught only if sampled and judgeable; and anything production changed after the bundle was built that the shadow missed, until the next bundle.

a server that predates the `fields` option of source-state returns only a document hash. that hash includes the source version, so against such a server every DID two independent backends hold shows as `document_hash_differs` and the corpus gate cannot pass.

**search** runs the canonical sets from [bench.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/bench.py), the [recorded candidate and PDS-scope cases](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/tantivy-dev-ui/candidate-smoke-cases.json), and any extra queries, against both backends directly. per query it reports identical ordered DIDs or the first differing rank, set overlap, actors whose response body differs, whether the scope acknowledgment headers agree, and whether bench.py's structural checks and each case's recorded expectations pass on each side. a differing query is run once more after a settle delay. a remaining difference is explained only when a DID the corpus phase flagged appears in either result.

it does not prove agreement on queries outside the set, and an explanation is an association, not a derivation: a flagged DID in the results does not show that it caused the difference. bench.py's spam guard needs Bluesky's answer and runs only with `--bluesky-oracle`. the public Worker path can be compared with `--production-public`, for information only, since its cache may be a minute old.

**latency** sends the same queries one at a time, alternating which backend goes first, once cold and several times warm. it records the backend's own `Server-Timing` search duration and the client-observed time, p50, p95 and p99 for each, and a paired per-query difference. the gate uses server time when every response carried it, otherwise client time. the report lists what was controlled and what was not; client time includes the network path from wherever the tool runs, so it compares the backends only when both are reached from the same region through the same kind of hop. nothing here measures behaviour under concurrent load.

**freshness** samples `/ready` and `/index/status` on both backends over a window: sync lag, watermark movement, moderation freshness, and the normalizer, plan, format and moderation hashes that must agree for results to agree. given a file of recently changed DIDs it also reads them from both admin listeners each sample and reports when the backends first agreed on each.

## load on production

a bundle places no load on production. every network target has its own request and byte budget and a fixed delay between requests; the tool stops when either budget is spent. nothing runs concurrently. the defaults allow 2,000 admin requests and 256 MiB per backend, which is a partial walk: a full walk has to be asked for with larger budgets, and resumes from `--checkpoint` when it stops. search, latency and freshness share 2,000 requests per backend at one request a second.

source-page and source-state each take at most 500 DIDs. a window of 500 DIDs costs one page and one state request per backend. measured on a local server with production-shaped records, that is 17.6 kB for the page and 230 kB for the state request and response with fields (82 kB without). each state request hydrates up to 500 stored documents on the serving machine, which is the real cost to production and the reason for the delay.

the arbiter makes one request per sampled DID to its authority and one per 25 DIDs to the appview, a second apart by default.

## inputs

`--production` and `--shadow` are the backends' public origins, needed for search, latency and freshness. `--production-admin` and `--shadow-admin` are their admin listeners, which bind loopback on the machine, so the corpus phase and `--changed-dids` must run where both are reachable. `--production-admin-secret-env` and `--shadow-admin-secret-env` name the environment variables that hold each admin bearer. the values are never printed or written to the report, are sent only to `/admin/` paths over https or loopback, and are redacted from error text.

`--phases` selects among `corpus`, `search`, `latency` and `freshness`. `--extra-queries` is a file with one query, or one JSON object of search parameters, per line. `--changed-dids` is a file with one DID per line. `--flagged-from` reads the flagged DIDs from an earlier report so search can run without repeating the walk. `--out-dir` is where the report goes; the default is a dated directory under [docs/evals](https://tangled.org/zzstoatzz.io/typeahead/tree/main/docs/evals).

## gates

| gate | passes when |
|---|---|
| (a) corpus | the walk completed and differing DIDs were re-read; every class has zero differences, or every sampled difference in it is labelled `shadow is right` or `both stale`. against a bundle that rule applies to `unchanged_in_shadow`; `newer_in_shadow` needs an arbiter sample with no `production is right`, and no `undetermined` for presence, handle or PDS; and at most a quarter of the shadow's records may have changed since the cutoff |
| (b) search | no check fails on the shadow, and every query whose results differ contains a flagged DID |
| (c) latency | for cold and warm, shadow p50, p95 and p99 are each at most production's value × 1.10 + 2 ms |
| (d) freshness | both backends ready, sync lag at most 120 s and moderation fresh at every sample, and each must-match value equal in at least one sample |

| constant | value | meaning |
|---|---|---|
| `ARBITER_SAMPLE_SIZE` | 25 | DIDs sampled per difference class |
| `ARBITER_PASSING_LABELS` | `shadow is right`, `both stale` | labels that do not fail gate (a) |
| `ARBITER_JUDGED_CLASSES` | presence, handle, PDS | classes that may not stay `undetermined` in `newer_in_shadow` |
| `SETTLE_DELAY_S` | 120 | wait before differing DIDs are re-read |
| `WATERMARK_SKEW_S` | 120 | how long before the watermark a shadow change still counts as newer than the bundle |
| `MAX_SHADOW_CHANGED_SHARE` | 0.25 | share of shadow records changed since the cutoff above which gate (a) fails |
| `MAX_TRACKED_DIFFERENCES` | 50,000 | differing DIDs kept for the re-read; any beyond it fail gate (a) |
| `LATENCY_NOISE_RATIO` | 0.10 | relative allowance in gate (c) |
| `LATENCY_NOISE_FLOOR_MS` | 2.0 | absolute allowance in gate (c) |
| `LATENCY_MIN_QUERIES` | 20 | fewer measured queries fail gate (c) |
| `FRESH_MAX_SYNC_LAG_S` | 120 | the cutover polling bound the server already reports |
| `FRESH_MIN_SAMPLES` | 3 | fewer samples fail gate (d) |

the latency allowance is a stated bound, not a measured noise level; no run against real backends has calibrated it. the watermark skew equals the sync lag gate (d) allows production and the changed share is a guess at what half a day of churn stays under; neither has been calibrated. `both stale` passes because the bar is the shadow being no worse than production. a gate whose phase did not run is `NOT_RUN` and the overall verdict is then `INCOMPLETE`. the exit status is 1 when any gate fails, 2 when the run could not finish, 0 otherwise.

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

against a bundle and the shadow's store, with no production endpoint involved:

```sh
uv run scripts/shadow_compare/compare.py --phases corpus --arbiter \
  --production-bundle views.jsonl --bundle-watermark 1793236000 --bundle-build-id b1793236000-example \
  --shadow-store https://SHADOW-STORE --shadow-store-secret-env SHADOW_STORE_SECRET \
  --checkpoint walk.json --corpus-max-requests 60000 --corpus-max-mib 16384
```

the [cloud runner](https://tangled.org/zzstoatzz.io/typeahead/blob/main/experiments/zkv-scale/README.md#shadow-comparison-runner) does the download, dump, sort and walk on a disposable machine.

## develop

```sh
just --no-dotenv test-shadow-compare   # builds typeahead-tantivy and dump-actors, runs unit tests and the integration test
```

# Small-PDS search experiment — 2026-10-08

**Local implementation and synthetic verification only. No production writes,
configuration changes, lexicon publication, or deployments.**

The [request](https://bsky.app/profile/renderg.host/post/3mxdrtvtnnk2q) is to search
within a community's hosting PDS, including members with custom-domain handles.
Filtering the global top results cannot recover community actors already excluded
by global candidate caps. The thread was read through Protopack's Slingshot and
Constellation skills.

## Implemented contract

`pds=acme.com` selects scoped prefix lists before the existing merge/rank/cap.
The global lists, result shape, and ranking rules remain unchanged. Both public
endpoint names accept the extension. `/search` and `/debug/search` also accept it.

- The offline builder counts **all known actor rows per normalized PDS**, including
  hidden and handle-less actors. A host qualifies at at most `INDEX_PDS_MAX_ACTORS`
  rows; default **10,000**, zero disables scoped lists. This is a snapshot-time
  cutoff, not an enforced live membership quota or a census of the entire PDS.
- Scope matching uses the indexed hosting hostname, not a handle suffix. ASCII
  case, HTTP(S) scheme, trailing slash, and trailing dot are normalized. Ports,
  paths, credentials, wildcard hosts, empty values, and non-ASCII names are
  rejected. Subdomains are separate; punycode hostnames are accepted.
- A host absent from the snapshot registry returns an empty list. A known host
  above the cutoff returns **503 `PdsScopeUnavailable`**. New PDSs become eligible
  at the next full build. A host that grows beyond the cutoff stays eligible
  until the next build.
- Updates create scoped keys only for eligible hosts. Migration diffs the previous
  and current PDS; hydration rechecks current membership. Global moderation,
  deletions, exact-handle promotion, long-query checks, and existing recall limits
  still apply. Unknown/invalid stored PDS endpoints remain globally searchable.
- Worker cache keys include normalized PDS. Missing/old backend support, a dead
  engine, emergency Bluesky proxy mode, and pending scope replay return 503.
  Scoped requests never fall through to global Turso/Bluesky search. The Worker
  requires `X-Typeahead-Pds` on a successful engine response, preventing an older
  backend that ignores the parameter from returning global results.

## Snapshot and overlay compatibility

Snapshots add `pds_scopes(host, actor_count, enabled)` plus `pds_scope_version=1`,
`pds_max_actors`, and `pds_scope_policy` metadata. Scoped keys append a control
separator and hostname to the ordinary key, preserving exact-key detection and
prefix-length top-N tiers. Actor blobs are not duplicated.

The policy is a hash of the sorted eligible-host set. When it changes, incremental
sync replays from the snapshot watermark instead of the later saved sync cursor.
Only a successful complete pass stores the matching policy in `sync_meta`.
Scoped requests remain unavailable until the two hashes match. This prevents
missing scoped keys for actors updated after the build's watermark but before
their PDS became eligible. Unscoped search remains available during replay.
A failed replay retries from the snapshot watermark; it does not advertise a
partial scope index. Unchanged eligibility does not trigger extra replay.

The isolated evaluation server marks its frozen fixture policy ready explicitly;
it runs neither background sync nor snapshot promotion and binds only loopback.
This is an evaluation affordance, not a production readiness shortcut.

## Verified locally

[Machine-readable measurements](pds-scope-results.json) use 100,005 synthetic
actors: 10,000 known rows on `acme.test`, 90,002 on `big.test`, and small adversarial
fixtures. Both builders use the same SQLite source. The baseline is commit
`46b0c63`, compiled separately in ReleaseFast. The reference snapshot contains only
Acme's rows. The actual HTTP search handlers, SQLite storage, overlay projection,
and serializers are exercised; the Worker handler runs in workerd/Miniflare with
real local D1 accounting and real Zig backends.

- **1,712 byte comparisons passed:** 214 queries × four limits, comparing scoped
  results with the PDS-only index and unscoped results with the old engine/index.
  Queries include common/rare prefixes, exact custom handles, multiple tokens,
  accents, CJK, long queries, misses, and limits 1/10/50/100.
- The community has no overlap with global top results for `bob`, yet scoped
  search recovers it. A handle ending in the community's domain but hosted
  elsewhere does not qualify. A subdomain-hosted actor stays separate.
- Real `/admin/refresh` calls verify migration from a snapshot actor, hiding,
  renaming, restoration, and moving to an excluded large host. SQLite inspection
  confirms no scoped overlay keys are generated for that large host.
- Removing the readiness marker makes scoped HTTP calls return 503 while global
  calls remain available. Restoring the matching marker re-enables the scope.
  SQLite tests also cover unavailable snapshot metadata, tombstones, exact scope
  membership, cutoff boundaries, normalization of PDS counts, and changed policy
  hashes. The full background replay against a real remote Turso source has **not**
  been exercised by this fixture.
- **18 Worker runtime checks pass:** normalized cache keys, cross-PDS/global
  isolation, API alias responses, actual cache hits, invalid scopes, excluded large
  hosts, empty scoped results with fallback enabled, old backends, dead backends,
  absent backends, and emergency proxy mode.
- Worker typecheck/lint pass; 97 Bun tests pass. Zig's index test runner passes
  129 tests with two opt-in real-data tests skipped; the ingest test runner is
  also successful. ReleaseFast service and evaluation-server builds pass.

| Measurement | Old global index | Small-PDS experiment |
|---|---:|---:|
| Snapshot bytes | 88,903,680 | 98,631,680 (+10.9%) |
| Emitted prefix memberships | 4,270,795 | 4,697,812 (+10.0%) |
| Distinct prefix keys | 870,378 | 967,490 (+11.2%) |
| Build wall time, one local run | 7.289 s | 7.329 s |
| Unscoped HTTP p50 / p95 | 0.272 / 0.636 ms | 0.263 / 0.625 ms |

Scoped HTTP p50/p95: **0.276 / 0.653 ms** (400 sequential requests).
At concurrency 20: **3.199 / 4.868 ms**, 400/400 successful requests.
Local update p50/p95: **0.632 / 1.410 ms** for an eligible PDS and
**0.492 / 1.028 ms** for an excluded PDS (200 writes each). These update timings
compare two paths in the candidate, not candidate versus baseline writes.

The earlier unrestricted prototype on a differently distributed synthetic fixture
produced a 213.9 MB snapshot versus 89.0 MB baseline. Restricting eligibility
avoids that large-PDS duplication, but savings depend on the real PDS distribution.

`just bench-search --url <loopback> --quick` passed. `just bench-ranking <loopback>`
was run and **failed**: the synthetic corpus lacks its named real-world actors,
so its structural and Bluesky-parity checks cannot pass. This is not a production
ranking assessment; the byte-for-byte old-engine and PDS-only comparisons above
are the relevant fixture correctness checks.

## Reproduce without production access

Use a fresh output directory. No credentials are needed. Never use a serving
SQLite file as the evaluation overlay.

```sh
python3 scripts/pds-fixture.py /tmp/pds-eval
cd services
zig build -Doptimize=ReleaseFast
zig build eval-server -Doptimize=ReleaseFast
MODE=indexer INDEX_CHANNEL=local INDEX_SOURCE=export INDEX_SAMPLE_LIMIT=0 \
  INDEX_PDS_MAX_ACTORS=10000 INDEX_REUSE_SOURCE=/tmp/pds-eval/source.db \
  INDEX_BUILD_ROOT=/tmp/pds-eval/full ./zig-out/bin/typeahead-ingester
MODE=indexer INDEX_CHANNEL=local INDEX_SOURCE=export INDEX_SAMPLE_LIMIT=0 \
  INDEX_PDS_MAX_ACTORS=10000 INDEX_REUSE_SOURCE=/tmp/pds-eval/acme.db \
  INDEX_BUILD_ROOT=/tmp/pds-eval/acme ./zig-out/bin/typeahead-ingester
```

Build `46b0c63` separately from `git archive`, using the same Zig version, source DB,
and build flags to produce the baseline snapshot. Copy only `eval_server.zig` and
its build target into that isolated baseline source to serve the old engine over
loopback. Do not replace the baseline's search/builder implementation. Then:

```sh
python3 scripts/eval-pds.py \
  --server services/zig-out/bin/eval-server \
  --baseline-server /absolute/path/to/baseline/zig-out/bin/eval-server \
  --full /tmp/pds-eval/full/build-REPLACE/index.db \
  --reference /tmp/pds-eval/acme/build-REPLACE/index.db \
  --baseline /tmp/pds-eval/baseline/build-REPLACE/index.db \
  --output /tmp/pds-eval/results --worker --standard-benchmarks
```

The harness starts isolated servers, performs comparisons and mutations, measures
latency, and shuts the servers down. `--worker` needs existing project dependencies.
The optional standard benchmark flag contacts Bluesky as a read-only oracle and
records its exit codes; the synthetic corpus is expected to fail ranking parity.
For a fully offline run, omit that flag. Check `standard_benchmarks` in the JSON;
its failures do not override the harness's separate differential assertions.

## Before any production rollout

The user explicitly requested **no production changes**. None were made.

A separate future rollout decision needs representative/full-corpus staging:
measure source counting, build RSS and scratch disk, snapshot size, update load,
replay duration and failure recovery, host eligibility changes during promotion,
and cold/multi-token lookup tails. These local timings are warm loopback results
on synthetic data, not guarantees for Fly, Cloudflare, or 40 million actors.
Large matching overlay prefixes may still require SQLite scanning/sorting; the
existing intersection and hydration limits are unchanged.

Future ordering: deploy compatible search/projection code; build and promote a
full scoped snapshot on an isolated channel; verify replay readiness and correct
small/large/unknown-host behavior; then enable the Worker parameter and publish
its lexicon only after staging acceptance. Older snapshots continue to support
global search, while scoped requests fail closed. Rollback must include assessing
cached scoped answers (up to 60 seconds) and replay eligibility; do not assume an
old search binary can maintain scoped overlay state for a newer snapshot.

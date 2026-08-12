# typeahead — agent guide

Community actor search for atproto. Discovery is the relay firehose
(`com.atproto.sync.subscribeRepos`), NOT jetstream — see `docs/discovery-and-identity.md`.
Canonical endpoint: `tech.waow.typeahead.searchActors`
(self-owned lexicon, `lexicons/`); `app.bsky.actor.searchActorsTypeahead` is served forever as
the drop-in migration alias — the two must stay byte-identical (smoke enforces this).
When endpoints change, update ALL public docs together: `/docs`, `/llms.txt` (src/pages/llms.ts),
README.md, homepage API chip. Service identity: @typeahead.waow.tech
(`did:plc:y6kywgnb7xletjpa5hhxmd4k`); lexicon publishing via `scripts/publish-lexicon.py`.
README.md has the architecture; this file is the operational contract for assessing the system.

**Before starting new work, read the git history** — `git log --oneline -30`,
then `git log -p` on anything that looks related. Commits are the record of
what is in flight: they carry the date, the reasoning, and the code together,
and they cannot drift from the tree the way a hand-maintained TODO list does.
Prefer them over any doc that claims to describe current state; when a doc and
the history disagree, the history is right and the doc needs fixing.

## the serving invariant

No request does work proportional to the actor corpus (~8M actors). The request path
reads a small set of precomputed prefix keys, merges the live overlay, hydrates a capped
list, and returns JSON. Any change that introduces corpus-proportional work on the request
or cron path is a regression — that exact mistake froze /stats once (full-scan `COUNT(*)`
in the hourly cron). Benchmark before declaring such work "fine".

## performance & benchmarking

Everything you need to assess performance is wired into the root `justfile`. Four layers,
one command each; `just perf` runs them all in order. Results land in
`scripts/bench-results.json`.

| command | layer it measures | what "good" looks like |
| --- | --- | --- |
| `just smoke` | **correctness gate** — response shape, ranking position, moderation, admin CRUD | all checks pass (non-zero exit on any failure) |
| `just bench` | **edge latency** — the worker + 60s CF cache, what users actually feel | warm p50 ≲ 60ms (cache hit); cold p50 faster than bluesky; mean overlap with bsky ≳ 75% |
| `just bench-search` | **engine latency** — the search service alone, bypassing the edge cache | warm p50 ≲ 100ms; cold varied-prefix queries should stay in the low hundreds of ms and beat bluesky's ~300ms |
| `just bench-ranking` | **ranking quality** — canonical query set vs bluesky as oracle, with `/debug/search` score breakdowns | structural criteria pass; parity criteria are aspirational (see below) |
| `just index-status` | **corpus scale** — `build_id`, `actor_count`, `prefix_count`, `normalize_version` | actor_count tracks the live corpus; build_id matches the deployed snapshot |

Defaults target prod (`typeahead.waow.tech`) and the search service
(`typeahead-search.fly.dev`); pass a `url=` to point elsewhere. Add `--quick` /
`--no-stress` to `bench`/`bench-search` for a fast pass. `bench-search` skips stress by
default so it doesn't hammer the single fly box.

**The benchmark oracle is bluesky, not a fixed number.** `scripts/bench.py` measures us
side-by-side against `public.api.bsky.app` for the same queries every run, so the bar moves
with the thing we're replacing rather than against a baseline that goes stale.

**Acceptance criteria are two-tiered** (`acceptance_checks` in `scripts/bench.py`):
- *structural* — must always pass. Wrong response, exact-handle mis-ranked (e.g. `zzstoatzz.io`
  must be rank 1 for `q=zzstoatzz.io`), display-name match missing, spam-dominated top 3.
- *parity* — bluesky's top result must appear in our top 8 for a named query set. These are
  expected to fail until authority-data coverage is real; that's deliberate, so PASS can't be
  bought by tuning lexical weights alone.

The canonical query set in `bench.py` is append-only by intent: once a query exposes a
ranking bug, it guards against that bug forever (this is where the quality_score-clobber
regression is pinned). Extend it, don't prune it.

### when to run what

- **after any ranking / scoring / index change** → `just bench-ranking` (gate) + `just bench-search`.
- **after a worker change** → `just smoke` runs automatically as the post-deploy gate of
  `just deploy-worker`; run `just bench` if the change could touch latency or caching.
- **after a snapshot rebuild / promote** → `just index-status` to confirm the new `build_id`
  is serving, then `just bench-ranking`.
- **periodic health** → `just perf`.

## deploys (smoke-gated)

- `just deploy-worker` — `wrangler deploy` **then** the smoke suite. A deploy that breaks
  shape, ranking, or the admin API fails loudly here. `deploy-worker-only` is the escape hatch.
- `just deploy-search` / `just deploy-ingest` — the Zig fly roles (ingest + search).
- **the snapshot builder (`MODE=indexer`) is NOT a Fly deploy.** It runs on the home box
  (heavypad) as the `typeahead-index` Prefect deployment in the `my-prefect-server` repo,
  scheduled every 3 days (09:00 UTC): reads Turso → builds the prefix-index snapshot →
  uploads to R2 → App B promotes. To change it, edit `flows/typeahead_index.py` /
  `prefect.yaml` there and redeploy. `prefect.yaml` IS authoritative for schedules —
  each deploy replaces them. `services/fly.indexer.toml` is retained only as a manual
  rollback path. Host prereqs: `deploy/home-indexer/install.sh`.
  See `docs/ephemeral-indexer-plan.md`.
- **`typeahead-plc-identity`** — the other heavypad Prefect deployment, weekly
  (Mon 05:00 UTC). Bulk DID -> handle/pds from the PLC log, for what `getProfiles`
  cannot see. See `docs/discovery-and-identity.md`.
- Deploy is manual (not tangled CI — the secret path is unreliable; CI only builds + tests +
  `wrangler deploy --dry-run`). So the smoke gate lives in the deploy recipe, not in CI.

## conventions

- worker tests: `bun test`. zig tests: `just test-services` (or `cd services && zig build test`).
- never run work proportional to the corpus on the request/cron path (see the invariant above).
- every equality query on `actors.handle` needs `COLLATE NOCASE`, or it scans the whole
  table — `idx_actors_handle` is declared with that collation. Paginate on `rowid`.
- Turso is single-writer and the live ingester shares it. Bulk writes must batch (one
  statement per batch, not per row) and pace against observed latency.
- ranking is by the stored `actors.quality_score`, not live followers; writes that touch
  aggregate counts must keep `quality_score` consistent with them (see the clobber regression
  guarded in `bench.py` + `smoke.py`).

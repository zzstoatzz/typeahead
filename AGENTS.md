# Typeahead — repository instructions

This root `AGENTS.md` is canonical. `.claude/CLAUDE.md` symlinks here so
Claude and other agents use the same instructions. Paths are repository-relative.

## documentation is part of the change

- Before changing behavior, identify affected README sections, operational docs,
  public API docs, diagrams, and agent instructions. Update them in the same task
  as the implementation; do not wait for Nate to notice drift.
- Before calling a task complete, compare README.md against the final diff and
  verified deployment state. Update it, or explicitly state why no README change
  was needed. Architecture, runtime roles, data flow, schedules, deployment,
  reliability, and performance guarantees all trigger this review.
- Distinguish shipped behavior, local tested changes, experiments, and proposals.
  A successful local test does not make a feature live. Date operational status
  and link its evidence; do not leave historical estimates labeled current.
- Keep the overview in README.md, detailed mechanisms in docs/, and durable rules
  here. Link shared material rather than maintaining duplicate instructions.
- Preserve existing user edits. Documentation checks supplement engineering
  review; no automated check can prove a prose claim is true.


Community actor search for atproto. Discovery is the relay's firehose
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
Use history together with the working diff and verified deployment state;
committed, locally modified, and deployed code may differ. Resolve discrepancies
and correct the docs rather than assuming one automatically describes production.

## the serving invariant

The target is predictable, bounded request work. The snapshot contains precomputed
prefix lists and the merge caps hydration at 50 candidates before exact promotion.
Union-prefix additions use the partial ranking index in this branch; exact keys
and intersections still read every matching row. The index is installed in the
production database; the schema initialization change is on this branch. See
[the lookup evaluation](docs/evals/bounded-overlay.md) for local experiments.

Avoid corpus-proportional request work and full-scan Worker cron selectors. Full
index builds are deliberately corpus-scale offline jobs; this is not permission
to move that work onto the request path.

## snapshot refresh ownership

Bulk builds and refresh orchestration belong to the cloud `typeahead-index`
deployment in my-prefect-server. Prepare and admit snapshots only on the inactive
managed Fly slot with independent storage. Never download, verify, resize or restart
the serving slot for a trial or refresh. Its slot pointer stays fixed while serving.
Promote only a qualified backend through the conditional R2 routing update; retain
the previous backend for rollback. Follow [the refresh runbook](docs/plans/automatic-snapshot-refresh.md).

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

Defaults target prod (`typeahead.waow.tech`) and the search backend
through the stable transport route (`typeahead-search-transport.n8-3e9.workers.dev`);
`smoke`/`bench-ranking`/`index-status` take a
positional url, `bench`/`bench-search` take `--url X`. Add `--quick` /
`--no-stress` to `bench`/`bench-search` for a fast pass. `bench-search` skips stress by
default so it doesn't hammer the serving Fly box. For engine-only timing, pass the
explicit backend origin; transport timing includes the routing hop.

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

- **after any ranking / scoring / index change** → local differential tests, then
  `just bench-ranking` + `just bench-search` against an explicit isolated target.
  Do not load-test production by default. For an optimization, require matching
  results against the old implementation on the same data, not just Bluesky parity.
- **after a worker change** → `just smoke` runs automatically as the post-deploy gate of
  `just deploy-worker`; run `just bench` if the change could touch latency or caching.
- **after a snapshot rebuild / promote** → `just index-status` to confirm the new `build_id`
  is serving, then `just bench-ranking`.
- **periodic health** → only when requested; do not start recurring monitors
  without authorization. `just perf` includes benchmarks, not just health probes.

## deploys (smoke-gated)

- `just deploy-worker` — `wrangler deploy` **then** the smoke suite. A deploy that breaks
  shape, ranking, or the admin API fails loudly here. `deploy-worker-only` is the escape hatch.
- `just deploy-ingest` deploys the Zig ingestion role. During the October 17 compact
  cutover, `just deploy-search` is guarded: `services/fly.search.toml` describes the
  retained legacy rollback backend. Restore its normal recipe only after the
  production compact service follows scheduled publication, using its verified
  configuration. See `docs/plans/compact-production-cutover.md`.
- **the snapshot builder (`MODE=indexer`) is NOT a Fly deploy.** It runs on the home box
  (heavypad) as the `typeahead-index` Prefect deployment in the `my-prefect-server` repo,
  configured every 3 days (09:00 UTC), temporarily inactive for the October 20
  cloud migration (see docs/operations.md): reads Turso → builds the prefix-index snapshot →
  uploads to R2 → App B promotes. To change it, edit `flows/typeahead_index.py` /
  `prefect.yaml` there and redeploy. `prefect.yaml` IS authoritative for schedules —
  each deploy replaces them. `services/fly.indexer.toml` is retained only as a manual
  rollback path. Host prereqs: `deploy/home-indexer/install.sh`.
  See `docs/operations.md`.
- **`typeahead-plc-identity`** — the other heavypad Prefect deployment, configured
  weekly (Mon 05:00 UTC), temporarily inactive with the bulk builder. Bulk DID -> handle/pds from the PLC log, for what `getProfiles`
  cannot see. See `docs/discovery-and-identity.md`.
- Deploy is manual (not tangled CI — the secret path is unreliable; CI only builds + tests +
  `wrangler deploy --dry-run`). So the smoke gate lives in the deploy recipe, not in CI.

## conventions

- worker tests: `bun test`. zig tests: `just test-services` (or `cd services && zig build test`).
- never run work proportional to the corpus on the request/cron path (see the invariant above).
- **the cron contract**: every cron selector must be an index SEARCH (smoke's
  `test_cron_selector_plans` EXPLAIN-guards this live against Turso), every batch size must
  state what it honestly covers against measured arrival rates (~2,900 new actors/hour), and
  the cron NEVER claims to drain a corpus-scale backlog — backlogs belong to the bulk paths
  (the `typeahead-enrich-backfill` Prefect deployment for profiles, `typeahead-identity-hourly`
  for new actors' handles, `typeahead-plc-identity` weekly for what the appview cannot see —
  all on heavypad, defined in the `my-prefect-server` repo), run off-worker. Since 2026-09-03
  the worker cron does NO enrichment at all: freshness, stats, and the moderation refresh
  only, inside a 12-minute budget (`CRON_BUDGET_MS`) so the 15-minute wall kill — which took
  120 of 140 runs, silently — cannot recur. Every run logs `cron_done` with per-phase ms.
- every equality query on `actors.handle` needs `COLLATE NOCASE`, or it scans the whole
  table — `idx_actors_handle` is declared with that collation. Paginate on `rowid`.
- Turso is single-writer and the live ingester shares it. Bulk writes must batch (one
  statement per batch, not per row) and pace against observed latency.
- traffic stats: `traffic_buckets` counts search cache-misses by app; `request_buckets` counts
  EVERY answer the worker gives (app × endpoint × status, router wrapper in `src/index.ts`) and
  feeds the `source · endpoint · status` views on /stats. `endpointLabel` is a fixed label set —
  unknown paths must collapse to `other`, never mint a new endpoint. Check `/stats?view=status`
  first when something smells; a 16% 500-rate on `/admin/ingest` was invisible before this.
- ranking is by the stored `actors.quality_score`, not live followers; writes that touch
  aggregate counts must keep `quality_score` consistent with them (see the clobber regression
  guarded in `bench.py` + `smoke.py`).


## PDS scope

`pds` filters hosting identity before candidate caps; never implement it by
filtering global top-N results. Global keys and unscoped responses must remain
unchanged. Scope keys, old-PDS suppression during identity migration, cache keys,
and fail-closed fallback behavior must be tested together. Snapshot metadata
`pds_scope_version=1` advertises support; the Worker also requires the search
response's `X-Typeahead-Pds` acknowledgment. The build-time
`INDEX_PDS_MAX_ACTORS` cutoff defaults to 10,000; excluded scopes must fail
explicitly. A changed eligible-host policy must replay from the snapshot
watermark before publishing its readiness hash. See `docs/evals/pds-scope.md` for
isolated evaluation commands and rollout order. No full-corpus capacity claim
may be inferred from synthetic fixture timings.

## experiment storage lifecycle

Every corpus-scale experiment must name its owner, scratch root, disk budget,
and cleanup path before writing data. Use scoped temporary directories and a
supervised process group with teardown on success, failure and cancellation.
Preserve small scripts, manifests, hashes and results; retire full database/index
copies after qualification and mark retired payload paths in the evidence record.
Do not declare an experiment complete while its servers or unbounded trial data
remain. Large files hard-linked into trial trees consume storage until their last
link is removed; verify reclamation with filesystem free space, not summed sizes.

# Typeahead — repository instructions

This root `AGENTS.md` is canonical. Shared repository skills live in
`.agents/skills/`, discovered directly by Pi and Codex. Claude Code discovers
the same skills through symlinks in `.claude/skills/`; keep those links when
moving or adding skills. `tools/oxlint/anti-slop` links to the installer skill’s
bundled plugin; edit that bundle so local lint and exported copies stay identical.
Claude Code 2.1.277 or later reads this `AGENTS.md`
directly when no ancestor `CLAUDE.md` overrides it. Paths are repository-relative.

## documentation is part of the change

- The README is an entry point: purpose, use, design, development and documentation
  links. Do not append task summaries, dated deployment inventories, release
  readbacks, incident narration or experiment results. Put those in the relevant
  existing docs; update the README when durable usage or architecture changes.
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


Community actor search for atproto. Accounts are discovered from the relay's
firehose (`com.atproto.sync.subscribeRepos`), NOT jetstream, which silently
drops records the relay carries.
Canonical endpoint: `tech.waow.typeahead.searchActors`
(self-owned lexicon, `lexicons/`); `app.bsky.actor.searchActorsTypeahead` is served forever as
the drop-in migration alias — the two must stay byte-identical (smoke enforces this).
When endpoints change, update ALL public docs together: `/docs`, `/llms.txt` (src/pages/llms.ts),
README.md, homepage API chip. Service identity: @typeahead.waow.tech
(`did:plc:y6kywgnb7xletjpa5hhxmd4k`); lexicon publishing via `scripts/publish-lexicon.py`.
[How it works](docs/how-it-works.md) explains the system, [operating](docs/operating.md)
has the procedures, and this file is the contract for changing it.
`docs/history/` holds the old Turso system and the move off it: read it for
reasoning, never for instructions, and do not link current docs into it.

**Check the Stream archive before designing any backfill.** [Stream](https://tangled.org/zat.dev/stream)
(our jetstream v2 port, live at `stream.waow.tech`, source in `~/tangled.org/zat.dev/stream`)
archives every repo record from the relay plus `#identity` and `#account` events
(`docs/jss-format-v1.md`, kinds 1–7) back to seq 1. Profile records, account
status history and DID→PDS hosting are there. Labeler labels (not repo records)
and appview aggregates (follower counts) are not: use the labeler
(`subscribeLabels`/`queryLabels`) and `getProfiles` for those. Read the local sources, not the live API:
`~/tangled.org/zzstoatzz.io/notes/atproto/record-retrieval.md` (which access path
fits; blocks are ~4,096 events, so drains are cheap and scattered per-DID reads
are not) and `network-backfill.md`, then Stream's `docs/configuration.md`
(archive endpoints need a bearer key; `/subscribe` replays 36 h) and
`docs/deploying.md` (archive keys).

**Before starting new work, read the git history** — `git log --oneline -30`,
then `git log -p` on anything that looks related. Commits are the record of
what is in flight: they carry the date, the reasoning, and the code together,
and they cannot drift from the tree the way a hand-maintained TODO list does.
Use history together with the working diff and verified deployment state;
committed, locally modified, and deployed code may differ. Resolve discrepancies
and correct the docs rather than assuming one automatically describes production.

## the serving invariant

The target is predictable, bounded request work. Search is answered by one of
two store slots on Fly (`typeahead-shadow-a`, `typeahead-shadow-b`): each
follows the network itself, keeps every account in a store on its own disk and
feeds a Tantivy index on the same machine with complete postings, global ranked
retrieval and durable live updates. Verify matching, ranking and PDS scope
against the deployed engine before changing retrieval.

Avoid corpus-proportional work on the request path and in the Worker cron. A
full resync of a slot's index from its store is deliberately corpus-scale; it
runs on the standby, never on the serving slot.

## slots and the route

One slot serves and the other stands by; the route record in R2
(`control/search-routing.json`) says which. Follow
[operating](docs/operating.md) for every one of these.

- Deploy, restart, resize or resync the standby only. To change the serving
  slot, flip the route to a fit standby first.
- Change the route only with `scripts/snapshot_refresh/store_slot_route.py`.
  Its admission and conditional write are the safety; never write the record
  by hand.
- Nothing pushes into a slot. A slot follows the relay, the PLC directory and
  the labeler and pulls policy and index requests from the Worker. It never
  holds a credential that can write anywhere else, and its store's port 8082
  never gets a public service.
- Both slots run one image. A change to the normalizer, plan version or index
  format cannot be admitted against the serving slot and needs its own plan.
- Bulk and backfill work runs in cloud infrastructure, never on HeavyPad or
  this laptop.

## production continuity during experiments

Before any serving or capacity experiment, check the live route, that the
serving slot is healthy and that the standby is fit
(`store_slot_route.py standby`), not just that machines are started. In an
outage, restore unscoped search first, with a failover or the
`EMERGENCY_PROXY_BSKY` switch, and diagnose afterwards. Recovery tests must
include an unavailable serving slot. Automatic fallback to Bluesky and PDS
scope safety must remain intact during changes.

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
| `just index-status` | **corpus scale** — `build_id`, `actor_count`, `normalize_version` | actor_count tracks the live corpus; build_id matches the route's |

Defaults target prod (`typeahead.waow.tech`) and the search backend
through the stable transport route (`typeahead-search-transport.n8-3e9.workers.dev`);
`smoke`/`bench-ranking`/`index-status` take a
positional url, `bench`/`bench-search` take `--url X`. Add `--quick` /
`--no-stress` to `bench`/`bench-search` for a fast pass. `bench-search` skips stress by
default so it doesn't hammer the serving slot. For engine-only timing, pass the
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
- **after a route flip** → `just index-status` to confirm the new slot's `build_id`
  is serving, then `just smoke` and `just bench-ranking`.
- **periodic health** → only when requested; do not start recurring monitors
  without authorization. `just perf` includes benchmarks, not just health probes.

## deploys (smoke-gated)

- `just deploy-worker` — lint and type-check, then `wrangler deploy` **then** the smoke suite. A deploy that breaks
  shape, ranking, or the admin API fails loudly here. `deploy-worker-only` skips smoke but still requires lint.
- `just deploy-search-transport` deploys the transport, then runs the smoke suite. A slot's
  origin must be in `src/search-route.ts` and deployed before a route record can name it.
- `just deploy-image-renderer` deploys the private PNG renderer, then validates
  fresh public images. Keep `workers-og` and its Wasm imports exclusive to that
  Worker. Deploy the renderer before introducing its public service binding.
- A slot image is deployed with `fly deploy . --config deploy/shadow-slot/fly.toml -a <standby app>`,
  standby first, then a route flip, then the other slot:
  [operating](docs/operating.md#upgrade-the-slots).
- Deploy is manual (not tangled CI — the secret path is unreliable; CI only builds + tests +
  `wrangler deploy --dry-run`). So the smoke gate lives in the deploy recipe, not in CI.
- The old ingester, the Turso-fed slots, the index refresh and the HeavyPad flows are
  retired and listed in [teardown](docs/teardown.md). Do not deploy, resume or delete
  any of them without Nate's explicit go; `just deploy-ingest` and `just refresh-search`
  belong to them.

## conventions

- worker tests: `bun test`. zig tests: `just test-services` (or `cd services && zig build test`).
  Retrieval changes also require `just --no-dotenv test-tantivy`, run by CI on main.
- never run work proportional to the corpus on the request/cron path (see the invariant above).
- **the cron contract**: the Worker cron does freshness, statistics, the control
  database probe and index-request pruning, inside a 12-minute budget
  (`CRON_BUDGET_MS`), because a 15-minute wall kill is silent. It does no
  enrichment and never drains a backlog: discovering, resolving and refreshing
  accounts is the store process's work on each slot. Every run logs `cron_done`
  with per-phase ms.
- the Worker reads and writes nothing in Turso for accounts (`PROFILE_MODE=slot`,
  `CORPUS_MODE=slot`, `TURSO_ACTORS=off`). Add no new Turso read or write. The
  Turso code that remains is listed for removal in [teardown](docs/teardown.md).
- traffic stats: `traffic_buckets` counts search cache-misses by app; `request_buckets` counts
  EVERY answer the worker gives (app × endpoint × status, router wrapper in `src/index.ts`) and
  feeds the `source · endpoint · status` views on /stats. `endpointLabel` is a fixed label set —
  unknown paths must collapse to `other`, never mint a new endpoint. Check `/stats?view=status`
  first when something smells.
  Statistics writers go through `recordStats`/`writeStats` (`src/stats-store.ts`) and
  statistics reads go through `statsReadDb`. `STATS_MODE=read-d1` reads the `STATS_DB` D1
  database and still writes Turso as well.
- moderation overrides, blocked domains and indexing requests have one source of truth, the
  `typeahead-control` D1 database, written only through `src/control-store.ts` (every policy
  write raises the revision in the same transaction). Store slots pull it; nothing pushes to a
  slot.
- profiles, the reputation report and the corpus counts on `/stats` are read from the routed
  slot: its index serves `/actors` and `/corpus` on its public port and the Worker reaches
  them through the transport (`src/slot-read.ts`). Anything a slot must publish goes through
  the index. Never refill descriptions or banners from Bluesky in bulk; the store refreshes
  each account on its own schedule.
- ranking is by the stored quality score, not live followers; writes that touch
  aggregate counts must keep the score consistent with them (see the clobber regression
  guarded in `bench.py` + `smoke.py`).


## PDS scope

`pds` filters hosting identity before candidate caps; never implement it by
filtering global top-N results. Global keys and unscoped responses must remain
unchanged. Scope keys, old-PDS suppression during identity migration, cache keys,
and fail-closed fallback behavior must be tested together. The index's
`pds_scope_version` advertises support and the route admission compares it between
slots; the Worker also requires the search response's `X-Typeahead-Pds`
acknowledgment. The engine supports every recorded canonical host. No full-corpus
capacity claim may be inferred from synthetic fixture timings.

## experiment storage lifecycle

Every corpus-scale experiment must name its owner, scratch root, disk budget,
and cleanup path before writing data. Use scoped temporary directories and a
supervised process group with teardown on success, failure and cancellation.
Preserve small scripts, manifests, hashes and results; retire full database/index
copies after qualification and mark retired payload paths in the evidence record.
Do not declare an experiment complete while its servers or unbounded trial data
remain. Large files hard-linked into trial trees consume storage until their last
link is removed; verify reclamation with filesystem free space, not summed sizes.

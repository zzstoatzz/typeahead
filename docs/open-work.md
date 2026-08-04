# open work

The durable list of started-but-unfinished and agreed-but-unstarted work.
This file is the handoff: a future session (human or agent) should be able to
pick any entry and act on it without re-deriving context from conversation
history.

## the paradigm

- One entry per piece of work. An entry carries four things:
  **context** (why this exists, with dates and links to code/docs),
  **state** (what is already done and verified),
  **next action** (the literal first command or edit),
  **verification** (how you'll know it worked).
- Entries are written at the moment context is freshest — when the work is
  deferred, not when it's remembered.
- Completing an entry means *deleting it* (git history is the archive), and
  updating any docs the work touched. A completed entry left in this file is
  a lie waiting to be believed.
- If an entry's premise stops being true, delete it and say why in the commit
  message.
- Ordering is by consequence, not recency: things that pay down risk or
  unblock other entries go first.

---

## 1. drain the never-enriched actor queue (backfill write runs)

**Context.** ~55% of the corpus (~6M actors, PLC-backfill and firehose era)
sits at `profile_checked_at = 0`, tied for the hourly cron's 200/run cold
floor — a ~3.5-year natural drain (measured 2026-08-04 by rowid sampling;
100% of rowid ranges 8M+ are unchecked). These actors are handle-searchable
but render with no avatar, no display name, score 0. Demand-driven hooks
(reputation on-view, request-indexing) heal only what people look at.

**State.** Prefect flow `typeahead-enrich-backfill` exists in
my-prefect-server (`flows/typeahead_enrich_backfill.py`), registered,
unscheduled, concurrency 1. Dry-run validated on 100 actors (97 resolved,
scores spot-checked against the formula). Pacing defaults: 25 DIDs/s
(one getProfiles call per second), 500-row select pages, 50-stmt paced
turso batches.

**Next action.** From my-prefect-server:
`just prefect deployment run 'typeahead-enrich-backfill/typeahead-enrich-backfill' -p limit=1000 -p dry_run=false`
Inspect the summary (samples, not_found rate), then chain larger runs with
`-p start_rowid=<last_rowid from prior summary>`. Full drain ≈ 2.5 days of
chained runs at default pacing; schedule paced chunks once trials look good.

**Verification.** Rowid-sample the zero count again (see 2026-08-04 session:
`SUM(CASE WHEN profile_checked_at=0 ...)` over rowid windows); search results
for long-tail actors show avatars/display names; the cron's cold floor stops
being a 6M-way tie.

**Caution.** The flow currently duplicates the canonical writer — see entry 2.
Fine for paced batches (formula was ported line-for-line and validated), but
entry 2 is the right end state.

## 2. backfill flow → pure orchestrator over the canonical writer

**Context.** `POST /admin/enrich-profiles` is the canonical enrichment writer
(getProfiles + extractProfileFields + full resolveHidden + guarded upsert;
see `docs/prioritized-enrich-plan.md`). The backfill flow reimplements that
logic in python (ported score formula, hide-only moderation) — exactly the
drift risk the endpoint exists to prevent, called out in its own docstring.

**State.** Worker side ready: `enrichProfileDids()` extracted and shared with
the reputation on-view path. Blocked on a secret: the flow needs the worker's
`ADMIN_SECRET` as a prefect block.

**Next action.**
1. `just prefect block register` a Secret named `typeahead-admin-secret`
   holding the wrangler `ADMIN_SECRET` value (operator step — the value is
   not readable from wrangler).
2. Rework the flow: keep the turso rowid-paging (read-only) and the
   bsky_404 stamping, replace compute+write with
   `POST {worker}/admin/enrich-profiles` in batches of ≤100 DIDs.
3. Delete `compute_authority_score` and the moderation constants from the
   flow.

**Verification.** A 100-actor run produces identical rows to the previous
direct-write path (spot-check quality_score, hidden, labels); flow no longer
imports/duplicates any scoring logic.

## 3. enrichment for appview-absent actors (tangled-only, PDS-only)

**Context.** All enrichment paths use bsky's `getProfiles`; actors the
appview has never ingested get `bsky_404` and stay skeletal forever. The
PDS-record fallback (`fetchProfileFromPds`, utils.ts — correctly normalizes
blob avatars to CIDs) is only reachable when a mod override is `show`.
Population: unknown size, includes tangled-only accounts. Their avatars now
depend on `avatarFallback` (PDS getBlob) since primaries are CDN-first.

**Next action (when it matters).** Decide whether bsky_404 + known PDS should
trigger the PDS fallback in `enrichProfileDids` (bounded: one getRecord per
404-actor, with the same countless-profile guard — a PDS profile carries no
counts, so score must stay 0/preserved).

**Verification.** A tangled-only actor's report shows display name + avatar
sourced from its PDS record.

## 4. request-indexing latency: synchronous turso write vs single-writer floods

**Context.** `POST /request-indexing` writes turso synchronously so its
"indexed" response stays truthful (59f6e39 lesson). Under firehose floods the
single writer queues ingester batch transactions ahead of it; measured
`turso_write;dur=10178` (2026-08-04), spikes past 45s. The smoke gate now
allows 60s for this check — calibration, not a fix.

**Options considered.** Moving the write off the response path reintroduces
the untruthful-"indexed" failure mode; splitting response ("accepted, landing
now") changes the UX contract. Revisit only if user-facing timeouts appear;
the endpoint's Server-Timing header attributes the phases.

## 5. zig deadline-fetch pattern → extract for reuse

**Context.** `services/src/db/TursoClient.zig` wraps fetches in a concurrent
task + 60s deadline + `Future.cancel`, after a kernel-stack capture proved
stalled TCP flows block forever in stdlib http (2026-08-04; see commit for
the capture). pub-search's `backend/src/db/Client.zig` and the pub-search
builder have the same exposure (its `verify_remote` hang, 2026-08-03, was
this class — bounded today only by watchdogs/timeouts).

**Next action.** Port the deadline-fetch pattern to pub-search's turso
client. Consider a shared zat helper if a third consumer appears.

**Verification.** A deliberately stalled connection (or the next natural
flood) produces a logged timeout + retry instead of a watchdog restart.

## 6. reputation report: percentile copy vs measured reality

**Context.** The staged progress calls sample-search ranking the slow part —
true since the sample positions moved to the live search engine the wait is
now sub-second, so the stages barely show. Harmless, but the stage timings
(2s/6s/20s) are tuned for a latency profile that no longer exists.

**Next action.** After a week of real use, re-tune or simplify the stages to
match observed p95 (likely: single "building report" state).

## 7. alert review: one page per signal, every page actionable

**Context.** 2026-08-01→04 produced pages from four systems (logfire
freshness, typeahead freshness bot, prefect crash automation, pub-search
watchdog). Fixed so far: pub-search freshness threshold 30m→60m (overnight
publish gaps), cancel-≠-crash orchestration (ln + pending upstream PR),
typeahead sync stall class. Not yet reviewed as a *system*: overlapping
alerts (logfire + typeahead bot both page on replica staleness), and no
alert covers the enrichment queue depth or the smoke gate's page-script
check failing outside deploys.

**Next action.** Inventory every alert across the four systems with its
trigger, threshold, and last three firings; kill or merge overlaps; add
missing coverage only where a firing would be actionable.

---

*Cross-repo note: the PrefectHQ/prefect branch `enforce-cancellation-under-force`
(cancel-≠-crash upstream fix, tests green) sits unpushed in
~/github.com/PrefectHQ/prefect awaiting the operator's go — tracked here
because ln's force-guard (`prefect-server` 3242906) is deliberately marked
"pending upstream" against it.*

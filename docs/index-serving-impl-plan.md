# index-backed serving + overlay — implementation plan (for review)

Milestones **B + C combined** (per the chosen approach: pure-index serving with
overlay semantics built in, not bolted on). Target bar for today: **canary** —
worker serves `/search` from our index with `EMERGENCY_PROXY_BSKY` still wired
as fallback. Backstop *removal* is later (needs benchmark). `USE_PREFIX_INDEX=0`
in prod until validated on search service directly.

Status: `merge.zig` is built + tested (81/81). Everything below is **proposed,
not yet written** — reviewing before I build it.

## What's already true

- search service has the full snapshot ATTACHed as `idx` (5.9M actors, 77.7M keys).
- `/search` is unchanged (FTS path), structurally isolated from `idx.*`.
- Overlay tables exist on search service's `local.db`: `actor_overlay(did, blob, hidden,
  updated_at)`, `prefix_overlay(key, did, score, op, updated_at)`.
- `prefixes.queryKeys` / `deriveAllUnique` give the shared key contract.
- `merge.combine(keysets, mode, cap)` folds base+overlay per-key sets into a
  ranked, deduped, capped DID list. union for single-token, intersect for
  multi-token; overlay op=0 suppresses, op=1 adds/rescore.

## Part 1 — serving read path (Milestone B)

New `index_search.zig`, routed from `handleSearch` when `USE_PREFIX_INDEX=1`:

```
norm  = normalize.query(q)
keys  = prefixes.queryKeys(norm)          # ≤3 single / N multi
mode  = merge.modeForQuery(norm)
lease = db.checkoutRead()                  # one lease: idx.* + local overlay
for key in keys:
    base = decode idx.prefix_top_base WHERE key=?      # []Scored via pack.DidListIter
    ov   = local prefix_overlay WHERE key=?            # []OverlayEntry
    keysets.append(.{base, ov})
dids = merge.combine(keysets, mode, hydrate_cap)
# hydrate: overlay actor wins over base; skip tombstone/hidden
for d in dids:
    a = actor_overlay WHERE did=?    # ?(blob, hidden)  — NULL blob = tombstone
    if a == null: a = idx.actor_compact_base WHERE did=?  # base blob
    if a == null or a.tombstone or a.hidden: continue
    results.append(present(unpackActor(a.blob), d.score))
sort by score desc, limit, write response (reuse search.zig writeActor shape)
```

- One read lease covers both `idx.*` (attached, immutable) and the local
  overlay tables (WAL). Batched `WHERE did IN (…)` for hydrate.
- Response shape identical to the FTS path so the worker/clients don't care
  which backend answered.

### open question 1 — ranking

Index score is the precomputed quality score (top-N already pruned by it). The
FTS path additionally re-ranks with lexical signals (exact-handle boost, prefix
match). **Proposal:** MVP uses index score + a small boost when the `e:`/`h:`
exact/handle key produced the hit, then compare against FTS in the bench and
refine only if quality regresses. Acceptable, or do we need full lexical
re-rank before canary?

## Part 2 — overlay updater (Milestone C)

Runs inside the sync loop (`sync.zig`) as it applies each actor delta from
Turso. Makes moderation/profile/delete/new-account **as fresh as sync**.

```
on actor delta applied locally (handle/display/hidden/deleted):
    if delta.updated_at <= snapshot.source_watermark: return   # base already has it
    if no snapshot attached: return                            # nothing to overlay onto

    new_h, new_d = normalize.handle/display(delta)
    new_keys = prefixes.deriveAllUnique(new_h, new_d)

    old = actor_overlay[did].blob ?? idx.actor_compact_base[did].blob
    old_keys = deriveAllUnique(old.handle, old.display) if old else ∅

    if delta.deleted:
        actor_overlay UPSERT (did, blob=NULL, hidden=0, now)        # tombstone
        for k in old_keys: prefix_overlay UPSERT (k, did, 0, op=0, now)
        return

    actor_overlay UPSERT (did, blob=pack(delta), hidden=delta.hidden, now)
    for k in (new_keys - old_keys): prefix_overlay UPSERT (k, did, score, op=1)
    for k in (old_keys - new_keys): prefix_overlay UPSERT (k, did, 0,     op=0)
    for k in (new_keys ∩ old_keys) if score changed: UPSERT (k, did, score, op=1)
```

### open question 2 — watermark gating

**Proposal:** only project deltas with `updated_at > snapshot.source_watermark`.
During full bootstrap / resync of old rows, skip overlay entirely (the snapshot
already reflects that state; projecting millions of bootstrap rows would be
huge and pointless). This bounds overlay to the 6–12 h freshness window. Agree?

### open question 3 — compaction on promote

When a new snapshot promotes (covers up to its newer watermark), overlay rows
older than the new watermark are redundant. Stale `op=1` adds could even be
wrong if the actor changed again. **Proposal:** on `swapSnapshot`, delete
`actor_overlay`/`prefix_overlay` rows with `updated_at <= new_watermark` (the
`idx_*_overlay_updated_at` index supports this). Tombstones/suppressions that
are still newer than the watermark survive — so we **cannot resurrect a
suppressed actor**. Prune-by-watermark vs clear-all — preference?

## Part 3 — concurrency (three actors on search service)

1. **sync loop** → writes `local.db` (actors, FTS, + overlay) via the WAL writer.
2. **serving** → reads `idx.*` + overlay via the read pool (WAL readers).
3. **promote watcher** → ATTACH-swap (close/reopen read pool).

- WAL handles concurrent overlay reader/writer.
- ATTACH-swap briefly closes the read pool → in-flight `/search` fails fast →
  503 → worker falls to backstop. Rare (per build). Acceptable for canary.
- **Known gap already flagged:** swap vs a concurrent full *bootstrap* both
  rebuild the pool — needs a shared pool-lifecycle lock before steady-state.
  Not triggered in the canary (bootstrap is done; swaps are post-bootstrap).

### open question 4 — moderation safety belt

Pure-index mode trusts overlay+base `hidden`/tombstone. Do we *also* cross-check
the live local `actors.hidden` at hydrate time as belt-and-suspenders (cheap PK
lookup we're already positioned for), or trust the overlay fully? Costs one more
`WHERE did IN (…)` against `actors`.

## Sequencing (today)

1. ✅ `merge.zig` (+ tests).
2. `index_search.zig` — read path + `USE_PREFIX_INDEX` route. Tests against an
   in-memory snapshot+overlay.
3. overlay updater in `sync.zig` (watermark-gated) + compaction in
   `swapSnapshot`. Tests for the diff/tombstone/rename cases.
4. Deploy search service; `USE_PREFIX_INDEX=1` on search service only; bench/poke `/search`
   (direct) vs FTS vs bsky for quality + latency.
5. Point worker `SEARCH_BACKEND_URL` → search service with backstop still wired (canary).

## 10x note

`actor_compact_base` grows ~linearly (≈50–85 GB at 59M) — the release valve is
a quality floor on emission (don't store a blob for actors that can't reach any
prefix top-N). Not in scope today; just not precluding it.

## Resolved (Codex review — green-lit with these rules)

1. **Ranking:** exact handle (`e:`) is a **hard win**, not a small boost — an
   exact `e:<query>` hit outranks everything. Implemented as a post-merge
   stable partition (exact DIDs first, each tier still score-ordered). Non-exact
   = index score; key-class tuning is bench-driven later.
2. **Watermark gating:** yes. Project only deltas whose **source** timestamp
   (`updated_at` / `deleted_at`, NOT local now) is `> snapshot.source_watermark`.
   Skip bootstrap. Operator/ad-hoc actions BYPASS this gate.
3. **Compaction:** prune **by watermark, never clear-all**, and only
   `source=sync` rows — **operator rows survive** (a manual hide/show must not
   vanish on promote). Add `actor_overlay(updated_at)` index for the prune.
4. **Moderation safety belt:** yes for canary — hydrate cross-checks the live
   `actors` table and skips missing/hidden DIDs (cheap PK batch; guards against
   overlay bugs while trust is earned).
5. **Tombstone invariant (explicit):** `actor_overlay.blob = NULL` is a
   **global DID tombstone** — suppresses a candidate even with no per-prefix
   `op=0`. rename = suppress old keys + add new; delete = tombstone + suppress
   old keys; recreate = non-null blob replaces tombstone + add new keys.

**Two hard requirements:**
- **Single transaction:** the actor-table write and its overlay write happen in
  the *same* local transaction — no "actor updated, overlay failed" split-brain.
- **Canary order:** do not point the worker at search service until direct search service tests
  pass with `USE_PREFIX_INDEX=1`. Backstop stays on regardless.

Schema add: `source INTEGER NOT NULL DEFAULT 0` (0=sync, 1=operator) on both
overlay tables; `actor_overlay(updated_at)` index. Operator API
(hide/show/ingest/explain) is out of scope for the canary — the `source` column
just future-proofs the prune.

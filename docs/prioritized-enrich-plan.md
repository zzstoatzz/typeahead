# prioritized enrichment — plan (for review)

> status: **proposed, reviewed once** (codex). dry-run script at
> `scripts/prioritized-enrich.py`; `--apply` is **explicitly blocked** until
> the new admin enrichment endpoint exists. validate the logic + thresholds
> before we touch Turso.
>
> **review outcomes (codex round 1):**
> - the original plan said `--apply` would write via `/admin/ingest` "so
>   `extractProfileFields` / `resolveHidden` stay in the loop." that was
>   wrong: `/admin/ingest` only handles jetstream-shaped events (DID,
>   handle, display_name, avatar_cid, updated_at, activity bump) and does
>   NOT write counts, `quality_score`, labels, hidden, created_at,
>   associated, or pds. write path corrected below.
> - threshold stays at `0.10`, but the report now also surfaces
>   `sum_followers` so we can move to a richer guard once we see it across
>   real PDSs.
> - kill switch is `--max-targets` (total writes), not `--max-pds`. default
>   `5000` for the first apply.
> - phase B sampling is now a deterministic spread across `listRepos`, not
>   the first N (was order-biased).
> - dry-run report dedups A ∪ B ∪ C and subtracts already-enriched DIDs
>   (qs > 0) so we don't celebrate a target count that's mostly no-ops.
> - phase C.5 (engaged-network) stays deferred. first run is A+B+C only.
> - bulk-enrich D and snapshot rebuild are explicit *separate* go/no-gos
>   after `--apply`, not in the same breath.

## problem

Popularity ranking on the prefix-index path is starved of data.

- 5,952,721 actors have a handle; **only 259 (0.0044%) have any `quality_score`**.
- The build-time top-N caps in `prefix_top_base` and the serve-time
  `HYDRATE_CAP` both sort by `quality_score`, so for every popular partial
  query they're tie-breaking 5.9M zeros, not ranking.
- Concrete: Paul Frazee (`pfrazee.com` — 308,569 followers per bsky's API) is
  `qs=0` in our DB, so he's buried on `pf`/`pfr`/`pfra` until the query becomes
  near-exact. Same shape for any genuinely popular account.

The refresh cron walks 1,000 actors/hour → ~245 days for a full pass. So most
accounts haven't been re-enriched since the `quality_score` computation
existed, and they never will be on this cadence.

A full bulk-enrich of the corpus (`scripts/bulk-enrich.py` at ~25 DIDs/s)
takes ~2.7 days and is the right *steady-state* mechanism. But naïvely walking
5.9M is the wrong place to *start* — it pays the same cost for the
dadavidtseng-shaped tail as for the head of the social graph.

## inspiration (the user's framing)

> "Start with the people I follow, then hop PDSes — every time you encounter
> a new PDS, index it all, unless it's `bsky.network` / `brid.gy` /
> `blacksky` / `eurosky` (defer those to the background)."
>
> "Maybe also the people that follow me."

The key insight: in atproto, your **follow graph** captures a wildly
disproportionate share of who anyone would actually search for, and the
**PDS your follows live on** is implicit endorsement of that community.
Mega-PDSs (bsky.network mainly) have millions of accounts but most are
long-tail; the indie atproto PDSs each have *tens to hundreds* of accounts,
overwhelmingly real people. So a prioritized walk extracts most of the
"useful" enrichment in minutes-to-an-hour instead of days.

## approach — four phases

```
  A. your direct follows                  (~501 accounts, minutes)
  B. each indie PDS we encounter          (~2,840 accounts, sampling guard)
  C. your followers via constellation     (~1,023 accounts, one batched call)
  D. bulk-enrich.py for the long tail     (~5.9M, ~2.7 days, background)
```

### Phase A — direct follows

1. Resolve the seed handle (`zzstoatzz.io` by default) → DID via slingshot.
2. Paginate `app.bsky.graph.getFollows` → list of `{did, handle, ...}`.
3. For each follow lacking `pds`, resolve via slingshot in parallel (10 threads).
4. Group by PDS, mark mega vs indie.

These are enriched *first* because they're the highest-signal accounts for
this user's typical searches.

### Phase B — indie PDS hop, with an engagement guard

For each non-mega PDS encountered in A:

1. `com.atproto.sync.listRepos` → enumerate DIDs hosted there.
2. **mega-threshold check**: if `listRepos` returns more than **5,000** entries,
   treat as mega → defer to phase D. (A "generic mega" guard for PDSs that
   aren't in the hardcoded skip list but turn out to be huge.)
3. **engagement guard**: sample 10 DIDs (configurable). For each, query
   constellation `getBacklinksCount(subject=did,
   source=app.bsky.graph.follow:subject)` → number of followers in the
   global index. Compute `engaged_ratio = (count ≥ 1) / sampled`.
4. If `engaged_ratio ≥ 0.10` (default after tuning — see § threshold below),
   enrich every DID on this PDS via bsky `getProfiles`.
5. Otherwise SKIP + log. This is the spam-farm guard
   (`dadavidtseng.com`-shaped: zero followers across the sample).

### Phase C — followers (via constellation, not bsky)

- `getBacklinks(subject=seed_DID, source=app.bsky.graph.follow:subject)`
  returns every DID that follows you, paginated.
- Single source, one batched call shape — faster + more complete than
  bsky's `getFollowers`, and pulls from the global atproto index (not just
  bsky's view).

### Phase D — bulk-enrich the rest (background, deferred)

After A/B/C land, `scripts/bulk-enrich.py` (existing) continues to enrich the
remaining corpus over ~2.7 days. The cron is the steady-state maintenance.

## microcosm services we lean on

| service | what we use it for | base |
|---|---|---|
| **slingshot** | identity resolution (DID/handle → `{did, handle, pds}`); already wired in worker | `https://slingshot.microcosm.blue` |
| **constellation** | global backlink index — gives us followers + engagement signal as cheap aggregated queries | `https://constellation.microcosm.blue` |
| ufos | not used here; ecosystem-discovery tool, future | `https://ufos-api.microcosm.blue` |

Constellation is the load-bearing addition: it replaces (a) paginating
bsky's `getFollowers` for phase C, and (b) hand-rolled handle-pattern spam
heuristics for phase B. Engagement-in-the-global-index is a far better
signal than "handle looks algorithmic" — the latter false-positives on
small communities and false-negatives on evolved spam.

## safety mechanisms

1. **Dry-run is the default**, no writes. `--apply` is opt-in. Iterate on the
   logic against real output before touching Turso.
2. **Mega-PDS skip list** by hostname substring: `bsky.network`, `brid.gy`,
   `blacksky.community`. Plus a generic `listRepos > 5,000` defer.
3. **Engagement guard** (§ Phase B step 3) — refuses to mass-enumerate a PDS
   that looks like a spam farm. False-positive impact bounded by the
   default 10% threshold (any PDS with at least 1 follower-having account
   in a 10-sample passes).
4. **Write-batching** in `--apply` (per existing `bulk-enrich.py` pattern):
   WRITE_BATCH=25 statements per Turso transaction, small pause between
   batches → short single-writer-lock holds, reads interleave. Avoids the
   write-contention that bit the domain-block backfill.
5. **Domain-block tooling as the relief valve**: if a PDS slips through the
   engagement guard and turns out to be spam after the fact, one
   `POST /admin/domain-block` hides the whole tree (we shipped this).
6. **Overlay path carries propagation**: every enriched account's bumped
   `updated_at` is projected into App B's `actor_overlay` /
   `prefix_overlay` within ~5 min of the next sync. So ranking improves
   live during the run; no snapshot rebuild required for individual
   results to appear. A *full* snapshot rebuild after the run bakes the
   new `quality_score`s into the build-time top-N caps and clears the
   accumulated overlay.

## the engagement threshold

Tuned against a real dry-run for `zzstoatzz.io` (68 non-mega PDSs):

- At `--min-engaged 0.30`, the guard SKIPped four legitimate small indie
  PDSs: `pds.pckt.cafe`, `npmx.social`, `margin.cafe`,
  `pds.scottlanoue.com` (manually verified: 9 accounts = operator with 434
  followers + a project + 5 test/alt/orphan).
- At `--min-engaged 0.10`, all four flip to ENRICH; no spam-shaped PDS
  appeared in the new ENRICH list. The narrowest pass is `npmx.social` at
  exactly 1/10.
- Floor: a *real* spam farm (e.g. dadavidtseng's 374 algo-handle accounts
  all with zero followers) would score 0/10 = 0% → still SKIPped at any
  positive threshold.

The longer-term improvement is to switch the metric from
`engaged_ratio = (count ≥ 1) / sampled` to **`sum(follower_count) > N`**
across the sample — a single high-follower account on a PDS is a stronger
signal than a fractional ratio. Noted for follow-up; the 0.10 ratio is the
right starting point.

## empirical dry-run (zzstoatzz.io, `--min-engaged 0.10`)

```
phase A enrichments:  501   (direct follows)
phase B enrichments: ~2,840 (61 indie PDSs ENRICH, 2 DEFER>5k, 1 SKIP-no-list)
phase C enrichments: 1,023  (followers via constellation)
TOTAL targeted:     ~4,364
bsky getProfiles calls (batch 25, 5 concurrent): ~175
estimated wall-clock: ~35 min
dry-run elapsed:    61 s
```

DEFER (over 5k threshold, correctly): `blacksky.app`, `eurosky.social`.
SKIP (listRepos failed): `at.nogrp.net`.

## `--apply` mechanics (gated on the new endpoint)

`--apply` does **not** route through `/admin/ingest` — that endpoint only
writes jetstream-shaped fields and is the wrong tool. Instead, a new
worker endpoint is the canonical enrichment writer:

### new endpoint: `POST /admin/enrich-profiles`

- **auth:** `Bearer ADMIN_SECRET` (same as the other admin endpoints).
- **body:** `{ profiles: [<bsky getProfile response shape>], reason?: string }`.
- **batch limit:** ≤ 50 profiles per request (a hard server-side max).
- **per profile:** run `extractProfileFields(profile, override?, domainBlocked?)`
  exactly as the existing enrichment paths do — so `resolveHidden`
  (label / slur / domain-block / per-DID override) and the
  `computeAuthorityScore` math live in **one** place. No mirrored Python
  scoring.
- **writes:** `INSERT ... ON CONFLICT(did) DO UPDATE SET …` with the full
  field set: handle, display_name, avatar_url, labels, hidden, created_at,
  associated, pds, followers_count, follows_count, posts_count,
  quality_score, `profile_checked_at = unixepoch()`,
  `updated_at = unixepoch()`. Insert path covers phase-B *discovery*
  DIDs we don't have yet.
- **returns:** `{ written, hidden, skipped, errors: [{did, reason}] }`.
- **idempotent:** safe to retry. We pre-fetch overrides + the blocked
  domains list once per batch and pass them in (same caller pattern as
  enrichment/cron already use).

### python `--apply` against the endpoint

1. Compute targets = A ∪ B ∪ C, **dedup'd**, **minus** DIDs already
   `quality_score > 0` in Turso. Cap at `--max-targets` (default 5,000).
2. Fetch bsky `getProfiles` in batches of 25, 5 concurrent (existing
   `bulk-enrich.py` pacing).
3. POST each batch of returned profiles to `/admin/enrich-profiles`.
4. Aggregate the `{written, hidden, skipped, errors}` counters; print a
   summary at the end + per-PDS rollup.
5. Stop on repeated 5xx from the endpoint or sustained `errors`.
6. App B's overlay picks up bumped `updated_at` within ~5 min via sync —
   ranking improves live during the run.

### tests for the endpoint (bun test against in-memory shape)

- Writes `followers_count` / `posts_count` / `quality_score` from a
  populated profile.
- Respects `hidden` from `resolveHidden`: label-hidden → hidden=1;
  domain-blocked → hidden=1; per-DID `show` override beats both → 0.
- Slur handle → hidden=1 regardless of qs.
- Empty profile → skipped, not zeroed.
- Insert path: a DID we don't have → row created with all fields.
- Update path: existing DID → updated, not duplicated.

## failure modes considered

- **A PDS lies about `listRepos`** (returns inflated or empty). Mitigation:
  if zero results, SKIP and log; if huge, the mega-threshold defers.
- **Spam farm with one shill follower** would pass at 1/10. Mitigation: the
  sum-of-followers metric (future) closes this; for the current threshold,
  the relief valve is `/admin/domain-block` after the fact.
- **Rate limit on bsky** during `--apply`. Mitigation: 5-concurrent, 200 ms
  pacing, and the existing `bulk-enrich.py` has handled this scale.
- **Turso write contention** during `--apply`. Mitigation: WRITE_BATCH=25,
  short transactions; the App B sync is read-mostly and re-reads cleanly.
- **Discovery overload**: phase B may insert thousands of *new* DIDs that
  weren't in our DB (good — that's the firehose-gap fix). Mitigation:
  these go through the same ingest path; the snapshot rebuild later picks
  them up. Until rebuild, overlay carries them.

## decisions deferred

- **Phase C.5 — engaged-network tier** (people who liked/replied/quoted/
  reposted your content). Constellation supports it trivially with OR'd
  sources. Worth doing after we see A+B+C results.
- **Sum-of-followers** metric replacing the ratio (see § threshold).
- **Snapshot rebuild** after `--apply` lands: kicks `prefix_top_base`'s
  build-time top-N caps off the now-real `quality_score` instead of
  random tie-breaks. Separate go/no-go via `INDEX_CHANNEL=prod
  INDEX_ALLOW_PROD=1`.

## review questions

1. Is the engagement guard at 0.10 the right starting threshold, or
   should we switch to sum-of-followers before `--apply`?
2. Should phase B include `--max-pds` (cap on number of indie PDSs walked
   per run) as a kill switch?
3. Should `--apply` write via `/admin/ingest` (HTTP, includes
   `resolveHidden`/domain-block) or direct Turso (faster, bypasses the
   worker)? I'm proposing the former for correctness.
4. Do we want phase C.5 in the first run, or keep it as a follow-up?
5. Order of operations: run `--apply`, wait for overlay propagation (~5
   min), verify a few queries improve (e.g. `pf` returns Paul ranked
   first), then kick D bulk-enrich + snapshot rebuild?

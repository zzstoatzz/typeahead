# Retrospective: 2026-05-23 standard.site backfill and production degradation

## What happened

A backfill of standard.site authors (11,089 DIDs) was carried out to close a
discovered coverage gap. During and after the backfill, production search
became user-visibly slow — multi-second tail latencies under concurrent load.

The backfill itself completed: 11,089 / 11,089 DIDs in the actors table,
10,759 (97%) fully enriched. The remaining 327 verifiably have no
`app.bsky.actor.profile` record on their PDS; 3 are abandoned `did:web`
domains. So the visible work product was correct.

But the degradation it surfaced was the more important finding, and it had
three latent contributors that compounded.

## Timeline

| Date | Event |
|---|---|
| 2026-05-07 | `cee16ec` — dot-skip cache rule added in `src/handlers/search.ts`. Motivated by chicago.at request-indexing case. |
| ~2026-05-12 | `SEARCH_BACKEND_URL` secret set on worker (per `wrangler deployments list` first deploy timestamp). |
| 2026-05-16 06:48 UTC | v73 deploy of the ingester. Built from `7287cc9` (poolio adoption commit). After this deploy, sync stops advancing its keyset cursor — see "Latent issue #2". |
| 2026-05-16–22 | `sync.query.updated` counts jump from ~120/day to 16,500+/day. Each "incremental" sync now scans the entire actors table instead of just the delta. OTEL exporter starts crashing on `defer span.end()` for the bootstrap span, sending the ingester into a crash loop. By May 19, no spans export at all. |
| 2026-05-23 morning | Investigation of cope.works (a did:web on goeo.lol PDS, not in our index). Discovered the wider "third-party PDS firehose blind spot" pattern; saved to memory. |
| 2026-05-23 afternoon | Investigation of bufo.uk (a did:plc on pds.zat.dev). Confirmed the blind spot is per-PDS, not per-DID-method. |
| 2026-05-23 evening | Backfill of 854 standard.site DIDs via `/admin/ingest`, plus identity + profile enrichment for ~1,750 partially-known DIDs. Added `site.standard.*` to ingester `wanted_collections`. |
| 2026-05-23 ~20:30 UTC | User reports production has been intermittently slow. Investigation begins. |
| 2026-05-23 ~21:00 UTC | Mitigation 1: `fly secrets unset LOGFIRE_WRITE_TOKEN` — bypasses the OTEL crash on `span.end()`. Sync now exits with `error.IoErr` instead of SIGABRT-looping. |
| 2026-05-23 ~21:25 UTC | Mitigation 2: `fly machine stop` ingester. Mitigation 3: `bunx wrangler secret delete SEARCH_BACKEND_URL`. p50 under 200-concurrent load drops from 10.6s → 110ms. |
| 2026-05-23 ~21:30 UTC | Three commits land + worker redeployed + ingester redeployed with parse-bug defensive fix. |

## Three latent issues, all in the request path

### (1) `src/handlers/search.ts` — dot-skip cache rule

```ts
// before
const useCache = !term.includes('.');
```

Every full handle contains a dot, so this rule disabled the 60s edge cache
for the most common autocomplete completion path. A user typing `nate`
gets the cache; the same user clicking through to `nate.bsky.social` does
not. Under any Turso write pressure (the ingester's continuous
`/admin/ingest` calls + the backfill scripts), uncached reads queued
behind writes and saw 5-6s tails.

Original motivation was that newly-indexed handles should surface
immediately, but the cost was disproportionate: 60s of cache opportunity
sacrificed for every full-handle search.

**Fix shipped:** commit `a8992d7`. Cache every search query for 60s.
Accept that newly-created accounts may take up to 60s to appear in
results.

### (2) The v73 sync regression — keyset cursor stuck at 0

After deploy v73 (May 16), the keyset cursor in both bootstrap and
incremental sync stopped advancing. Ingester logs showed:

```
sync: batch: 2000 rows, rowid=0, total=44906000
```

with `rowid=0` on every iteration. `total` grew unbounded. Logfire
spans confirm the same: `sync.query.updated` jumped from ~120/day
pre-deploy to 16,500+/day post-deploy — each "incremental" sync was
re-scanning the entire actors table.

The symptom is `last_row.int(trailing_col)` returning 0 for the rowid
(bootstrap) and `updated_at` (incremental) columns. The next query
becomes `WHERE rowid > 0` or `WHERE updated_at > 0`, which matches
everything → loop reads same 2000 rows forever.

In parallel, the OTEL exporter started crashing in
`otel/api/common/attributes.zig:560 initOwnedSlice` when ending the
long-running `sync.bootstrap` span, sending the ingester into a SIGABRT
crash loop. The crash masked the sync-cursor bug — every restart
re-entered bootstrap and re-triggered the loop.

The actual root cause of `int(trailing_col) → 0` is **not yet
identified**. Standalone reproductions of `extractInt` against the
exact Hrana wire format (verified by curl against production) return
the correct values in both Debug and ReleaseSafe. The defensive
rewrite + diagnostic in commit `983b801` protects against unknown
shapes but does not explain the original bug.

**Mitigations shipped:**
- `fly secrets unset LOGFIRE_WRITE_TOKEN` — bypasses OTEL crash; sync
  now exits early with `error.IoErr` instead of crash-looping.
- Defensive `extractInt` + one-shot diagnostic log in
  `services/src/db/TursoClient.zig` (commit `983b801`).

**Not yet fixed:** root cause of the parse bug. The diagnostic log
will tell us next time it happens.

### (3) `SEARCH_BACKEND_URL` — the trap that did the user-visible damage

`src/handlers/search.ts:48-73`:
```ts
if (env.SEARCH_BACKEND_URL) {
  const backendRes = await fetch(backendUrl, {
    signal: AbortSignal.timeout(3000)
  });
  if (backendRes.ok) { /* use backend */ }
}
// fall through to turso
```

The worker tries the ingester (`SEARCH_BACKEND_URL`) first for every
cache-miss search, with a 3-second timeout, before falling back to
Turso. Healthy ingester = sub-ms backend hits. Unhealthy ingester =
3-second wait per request, every request.

When the ingester started crash-looping after v73 (which itself was
triggered/caused by issue 2), the worker began eating 3s per
cache-miss request. Under concurrent load, p50 ballooned to 10.6s.
The 3.1s minimum I measured later is the exact `AbortSignal.timeout`
value — that was diagnostic but I missed it for a long time.

**Fix shipped:** `bunx wrangler secret delete SEARCH_BACKEND_URL`.
Worker now goes directly to Turso on cache miss. p50 dropped from
10.6s → 110ms under 200-concurrent.

**Not shipped (follow-up):** the search.ts code still has the
`if (env.SEARCH_BACKEND_URL)` branch. If anyone sets the secret again
without ensuring the backend is reliably healthy, the trap reopens.
Should add: circuit breaker, much shorter timeout (e.g. 200ms), or
healthcheck precondition.

## Contributing factor: my backfill writes

The standard.site backfill ran multiple bulk write batches against
Turso (854 bare-DID inserts in one batch, then ~1,750 enrichment
updates in batches of 50). On a healthy system this is trivial. On a
system already in latent issue (2)'s read-storm state, these added
enough write-lock contention to push tail latency from "noticeably
slow" to "user-visible breakage". The straw, not the load.

## Misdiagnosis I went through

I want this documented because it's the most useful part for the
safe-backfill skill.

1. **"jetstream filters did:web"** — wrong framing. The pattern is
   per-PDS, not per-DID-method (bufo.uk being did:plc and equally
   absent proved it). Saved to memory under
   `project_did_web_discovery_gap.md`.
2. **"35h cursor backlog catch-up is the cause"** — partially right
   (ingester writes contributed) but I missed `SEARCH_BACKEND_URL`
   entirely. The cumulative write rate alone wasn't enough.
3. **"Turso quota throttling"** — flat wrong. Turso forgave the
   overage ("Vegas Blackout" message in the dashboard); behaviorally
   identical to staying under quota. Should have web-searched
   Turso's actual overage behavior before asserting.
4. **"Cache going to Turso, just slow"** — missed the
   `SEARCH_BACKEND_URL` branch entirely. Reading the search handler
   end-to-end on day one would have surfaced it.

The thing that broke (3) open was the user's instruction to load
the worker like real users do — 100+ concurrent unique queries, not
50 sequential. The 3.1s minimum was the telltale.

## What's still open

- **Root cause of `int(trailing_col) → 0` in production.** Cannot
  reproduce locally. Defensive fix is in place; one-shot diagnostic
  will name the specific value next time it manifests.
- **OTEL `span.end()` segfault.** `otel-zig` `initOwnedSlice` crash
  in attributes path. We bypassed by unsetting
  `LOGFIRE_WRITE_TOKEN`; ingester is now blind to logfire. Needs
  investigation in `../otel-zig` or `../logfire-zig`.
- **Whether `SEARCH_BACKEND_URL` should ever come back.** Local
  replica serving search is a good idea in principle. The current
  failure mode (slow backend → user-visible amplified slowness) is
  not acceptable. Needs circuit breaker or much shorter timeout.
- **Fixture-based test of `extractInt`** inside the ingester crate,
  so any wire-format drift regresses loudly rather than silently
  becoming zero.

## Lessons for the safe-backfill skill

1. **Map every routing path before backfilling.** Audit env vars and
   secrets on every service in the request path. A stale fallback
   URL like `SEARCH_BACKEND_URL` can amplify backend slowness into
   user-visible latency by 30-100x.
2. **Baseline must be under realistic load.** "5 sequential queries
   returned in 200ms" is not a baseline. 100+ concurrent unique
   queries (every one a cache miss) surfaces routing, timeout, and
   lock issues that single queries hide.
3. **Backfill writes compound with existing system state.** Same
   backfill on a healthy system is trivial; on a system in any kind
   of internal loop or contention it's the trigger. Pre-flight
   checks:
   - Is sync actually advancing (look at last_sync, span counts)?
   - Is Turso write-lock time healthy?
   - Are spans completing (not stuck)?
4. **Treat any unexplained minimum latency as diagnostic.** If
   `min(latency)` for a series of independent requests is suspiciously
   round (3.0s, 5.0s, 30.0s), it's almost certainly a hardcoded
   timeout somewhere. Grep the codebase for that value.
5. **Verify by web-search before asserting platform behavior.**
   "Quota throttling" was a confident wrong claim. A 30-second web
   search would have surfaced Turso's actual overage policy.
6. **One-shot test ≠ verified fix.** I shipped the dot-skip removal,
   tested one warm/cold pair, declared success. The user immediately
   broke it again with concurrent load. Always re-load-test after
   a fix, not just a smoke-test.
7. **Don't trust silent fall-throughs in data decoders.** The
   original `extractInt` had `else => 0` as the inner fall-through.
   That mapped "unknown shape" to "valid zero," which became a
   forever-loop in the cursor-advancing code. Defensive decoders
   should either recurse on uncertainty or log loudly, not silently
   return a sentinel that's also a valid value.

## Commits from this session

- `a8992d7` — worker: drop dot-skip cache rule; cache every search query for 60s
- `0d21010` — ingester: subscribe to site.standard.* via NSID prefix wildcard
- `983b801` — ingester: harden extractInt against unexpected hrana value shapes
- `bunx wrangler secret delete SEARCH_BACKEND_URL` (no commit; secret-only change)
- `fly secrets unset LOGFIRE_WRITE_TOKEN` (no commit; secret-only change)

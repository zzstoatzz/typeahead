# Retrospective: 2026-06-12 → 06-15 ingester wedge, recovery, and throughput root cause

## What happened

The Zig ingester silently stopped ingesting for **13 days** (May 29 → Jun 11)
without alerting. A user noticed the stats chart had flatlined. The immediate
cause was trivially fixable (a restart), but the recovery that followed took
**three days and far too much floundering** — chasing a data-backfill symptom
for two of those days while the actual recurring defect (the ingester can't
keep up with the firehose because its flush blocks its read loop) went
undiagnosed until it forced itself into view at 4am on day three.

The search service — the thing users actually hit — was never down. Every
incident here was in the ingest/freshness path, insulated from serving by the
snapshot+overlay architecture. That same insulation is *why* the 13-day wedge
went unnoticed: nothing user-facing broke.

## Timeline

| Date (UTC) | Event |
|---|---|
| 2026-05-29 ~16:00 | Process-wide TLS rot in the ingester: every outbound TLS handshake starts failing `TlsInitializationFailed` — both the TursoClient and the worker-flush HTTP client (they share one `std.Io`). Every flush fails; the backlog-overflow path drops buffered events and advances the cursor. |
| 2026-05-29 → 06-11 | The jetstream websocket (established before the rot) keeps delivering for 13 days while every flush fails. ~190K+ new accounts and all profile/identity events in the window are dropped. Errors are `log.err` console-only — invisible to logfire. No alert exists. |
| 2026-06-11 ~15:58 | The websocket finally drops. Reconnect needs a fresh TLS handshake → impossible. Process fully inert except a 5-min Turso retry loop. |
| 2026-06-12 ~17:13 | User: "are we not ingesting new actors?" (flat stats chart). |
| 2026-06-12 17:18 | `fly machine restart` → instantly healthy, resumes from cursor. Jetstream's ~25h replay window clamps the resume to Jun 11, so May 29 → Jun 11 is unrecoverable from the stream. |
| 2026-06-12 | Shipped guardrails: liveness watchdog (no event/flush for 10min → `exit(1)` → fly restarts), `ingest.heartbeat` span + logfire→discord absence alert, flush keep-alive, `MAX_BATCH` 100→300. |
| 2026-06-12 | Found role flags defaulted all-on → the ingester had been running the search app's full-corpus sync vestigially. Refactored to a required explicit `MODE`; deleted the dead dual-write path. |
| 2026-06-12 | Replay race unwinnable (server paces historical replay) → jumped the KV cursor to live, decided to recover the gap via a PLC-directory export backfill. Built `scripts/backfill-plc.py`. |
| 2026-06-12 → 06-14 | Backfill flailing (see below). First run stalled early and **sat dead for ~2 days unnoticed** — discovered ~1% complete via a spot check on Jun 14. |
| 2026-06-14 evening | Sharded the backfill 4× for speed. It did **not** 4× (shared single-writer ceiling) and pushed the live ingester behind. Rolled back to 2 shards, then killed all. |
| 2026-06-15 ~04:00 | With backfill fully stopped, the live ingester **still** caught up at only 0.69–0.8×, gap at ~2.9h and growing. Root cause finally diagnosed (host roulette + synchronous flush). Fixed: pin jetstream host + `MAX_BATCH` 300→1500 → 1.8× catch-up, gap draining. |

## Root causes

### 1. The original wedge — long-lived `std.http.Client` TLS rot
A single process-lifetime HTTP client's TLS/pool state rotted after ~5 days of
uptime; every subsequent handshake failed permanently. Covered in
`memory/project_ingester_tls_wedge.md`. The 13-day blindness was the real
failure: no freshness alert, console-only errors, and an architecture that hides
ingest death from users.

### 2. The recurring throughput defect — synchronous flush + host roulette
This is the important one, because it had bitten us before (the Jun 12 catch-up
also stalled <1×) and we kept treating its symptoms.

- **Synchronous flush blocks the read loop.** The flush POST to the worker
  (~1–2s round trip) stalls the jetstream consumer — it reads nothing during the
  POST. Throughput ceiling = `MAX_BATCH / flush_time`. At batch 300 this capped
  at ~0.8× of peak firehose *even on a fast host*. The tell, which we should have
  read immediately: **tiny flush batches with `pending: 0`** = receive-bound /
  read-loop-stalled, not write-bound.
- **Host roulette.** `zat.JetstreamClient` round-robins 12 default hosts
  (official + community mirrors) on each reconnect. Flapping landed it on the
  `fire.hose.cam` mirror, which dribbled the catch-up replay sub-real-time.

Fix: pin `.hosts` to `jetstream.waow.tech` + official bsky fallback (drop
mirrors), and `MAX_BATCH` 300→1500 as a stopgap. **The proper fix is still
open:** decouple the reader from the flusher (reader fills a queue, writer
drains it) so a POST never stalls firehose reads. See
`memory/project_ingester_throughput.md`.

### 3. Surfaced during recovery — overlay bloat from a stale snapshot (q=bsky 210s)

While health-checking, `q=bsky` was found to take **210s on App B / 500 on the
worker**, while every other query was sub-second. Verified mechanism: prefix
search reads, per key, `prefix_top_base` (snapshot, capped 100) ∪ `prefix_overlay`
(live changes since the snapshot). But `overlayPrefixRows` is
`SELECT ... FROM prefix_overlay WHERE key = ?` with **no LIMIT**, and the overlay
updater inserts one row per (key, changed-DID) with **no per-key cap** (the
`overlay_top_n:100` caps only the snapshot build). The serving snapshot was
**19 days stale** (built May 27, never rebuilt — rebuilds were never automated),
so the `bsky` key — which nearly every `*.bsky.social` account maps to —
accumulated millions of overlay rows, and the request read all of them. A
serving-invariant violation, invisible until the overlay bloated.

This is the **third instance of one root shape**: a load-bearing invariant
assumed but neither enforced in code nor monitored. (1) "ingester stays alive" —
no freshness alert → 13-day wedge. (2) "catch-up beats realtime" — no cursor-lag
alert → silent slowdown. (3) "overlay stays small (snapshot fresh)" — no
staleness alert, no defensive cap → 210s. Same failure, three times.

**Important correction (don't soften this):** the overlay bloat was NOT an
unforeseen design gap. The May rearchitecture doc
(`2026-05-25-typeahead-rearchitecture-notes.md`) designed the rebuild cadence as
the overlay-bounding mechanism — overlay holds only deltas newer than the
snapshot watermark, "compacted on promotion," with principle #6 "Some Lag Is
Fine, Permanent Drift Is Not (self-correcting convergence)." It then listed
**"Schedule and monitor recurring index builds"** explicitly under *Remaining
Work* ("not blockers for cutover, but important"), alongside "Ensure R2 tokens do
not expire" and "Keep measuring sync lag and overlay advancement." So this was a
**known, written-down, deliberately-deferred post-cutover task that was never
closed.** ~3 weeks later it failed exactly as the absence of it predicts. The
overlay-read `LIMIT` is the one genuinely-new gap (the design assumed the overlay
stays small via cadence, so never specified a per-key read bound). The lesson is
about *closing deferred-but-required operational TODOs*, not about foresight.

## What went wrong in the *response* — the floundering

The user's actual complaint. Honest accounting:

1. **Chased the symptom, not the root cause, for two days.** The slow catch-up
   was visible on day one (keep-alive + batch tweaks nudged it 0.8→0.94× and I
   moved on). Instead of recognizing that as a throughput *ceiling* and fixing
   it, I poured two days into a PLC backfill — recovering lost *data* — while the
   live *freshness* defect sat undiagnosed. The backfill was the less important
   problem.
2. **Over-claimed before verifying.** Declared "dead for 2 weeks" on a first-pass
   histogram that used `updated_at` (write time), not insert time; had to walk it
   back when the user pushed ("there's absolutely no way"). The authoritative
   signal (`actor_deltas`) was one query away.
3. **Optimized the wrong layer, twice.** Sharded the backfill walk against a
   single-writer Turso (parallel walkers can't beat a serialized write ceiling,
   and they starved the live path). Bumped batch sizes piecemeal instead of first
   classifying receive-bound vs write-bound — which the `pending:0` signal
   answered for free.
4. **Babysat a background task at the live path's expense.** Repeatedly ran
   machine surgery on the low-priority backfill, twice pushing the live ingester
   behind, instead of cleanly parking background work and protecting the
   live-critical path.
5. **Communicated in machine-level play-by-play.** The user had to ask "where are
   we / be clear" four times. Status should lead with the plain-English bottom
   line (what's broken, what's at risk, what I'm doing), not a tour of machine
   IDs.

## Action items

- [ ] **Decouple ingester reader from flusher** — the real throughput fix; the
      recurring ceiling behind every "can't keep up" episode. Daylight work.
- [ ] **Freshness alert on the data, not just the heartbeat** — alert when
      `max(ingest cursor)` lags real time beyond a threshold, so a *slow* ingester
      (not just a dead one) pages before the gap nears the replay window.
- [ ] **Cap the overlay read** — `overlayPrefixRows` needs a score-ordered
      `LIMIT` (preserving op=0 suppressions) so a stale snapshot degrades
      gracefully instead of 210s. A first attempt (commit d8427ec) was REVERTED
      (2cd3242, 2026-06-16): its supporting `CREATE INDEX` ran in `LocalDb.open()`
      before the HTTP listener and blocked App B boot on the bloated table. Re-do
      with the index built OFF the boot path (background, post-listen) before any
      redeploy. Not urgent — the daily rebuild + overlay-prune keeps q=bsky fast;
      this is defense-in-depth for the prune-lag window.
- [x] **Automate the snapshot rebuild cadence** — DONE 2026-06-15: fly
      daily-scheduled machine `index-rebuild-daily` (typeahead-indexer) builds +
      prod-promotes daily; App B auto-adopts. Closes the rearchitecture doc's
      deferred "schedule and monitor recurring index builds" TODO. See
      `docs/operations.md`.
- [ ] **Alert on snapshot age / overlay size** — page before users hit slowness.
- [ ] **Add a high-cardinality prefix (q=bsky) latency check to the bench/smoke**
      append-only query set — so a bloated overlay fails the gate, not found by luck.
- [x] Liveness watchdog + heartbeat absence alert (shipped 2026-06-12).
- [x] Pin jetstream host; drop community mirrors (shipped 2026-06-15).
- [ ] Snapshot rebuild + promote to clear the 19-day staleness (queued after backfill).
- **Process lessons (the point of this retro):**
  - When something "can't keep up," classify **receive-bound vs write-bound
    first** (`pending` + batch fullness tell you) before optimizing any layer.
  - **Never parallelize against a shared single-writer.**
  - **Verify the authoritative metric before declaring** anything.
  - **Separate live-critical from background-cleanup explicitly**; park the latter,
    never let it risk the former.
  - **Lead status with the plain-English bottom line.**

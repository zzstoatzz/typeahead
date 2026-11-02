# Tantivy slot expired after one missed refresh

Status: availability restored November 2, 2026 at 18:21 by native rollback.
Times below are UTC. This is the same expiry chain as the
[October 31 outage](2026-10-31-search-expiry-outage.md); the October fixes kept
unscoped search up, but did not stop the chain itself.

## What happened

| time (Nov) | event |
| --- | --- |
| 1, 18:03 | run `0165b29f` takes the checkpoint for build `b1793556191`; it promotes slot A at 19:40. A's coverage ends 24 hours after the checkpoint. |
| 2, 05:59:55 | scheduled run `1b11907a` crashes at submission: Hetzner `POST servers` answered HTTP 412 `resource_unavailable` for `cpx32` in `hel1`. Nothing retried it. |
| 2, 06:14 | slot B (previous build) stops at its own coverage deadline; it is not serving. |
| 2, 18:02 | slot A reaches its coverage deadline, stops itself, and Fly gives up after three restarts. |
| 2, 18:02 → 18:21 | the Worker fails over unscoped searches to Bluesky after its 3 s primary timeout (3.3–3.4 s per search). PDS-scoped searches return 503. |
| 2, 18:03 | scheduled run `f83204de` fails in 20 s: a `ReadTimeout` on dead A's `/health` while checking the current slot before building a replacement. |
| 2, 18:21 | rollback run `a54aa307` routes to the native anchor `typeahead-startup-20261024` (build `b1793109259`). Search returns native results in about 0.3 s, scoped searches work again, and `/health/freshness` reports `stale` (Oct 27 snapshot). |

## Root cause

A Tantivy slot serves for 24 hours after its checkpoint. The publisher runs at
06:00 and 18:00, and a refresh takes about 1.7 hours to reach promotion. So only
the 06:00 run can renew a slot taken at 18:00; the following 18:00 run promotes
after the deadline. **One missed run is an outage.** There is no margin.

Three defects turned one cloud capacity rejection into an outage that also
blocked recovery:

1. **Provisioning had one placement.** The builder asked for exactly `cpx32` in
   `hel1` and treated a capacity rejection as final. The October retro moved the
   publisher from `fsn1` to `hel1` after a 412 there. That swapped one fixed
   location for another; it was not a fallback.
2. **Nothing retried a crashed run.** A run that crashes at submission waits 12
   hours for the next schedule. That is past the deadline.
3. **A dead serving slot deadlocked refresh.** Automatic continuation checked the
   current slot's health before building. A dead slot failed that check, so the
   run that could replace it never started. Every later run would fail the same
   way while the route pointed at it. The October retro said "promotion does not
   require a dead source backend". That was true of promotion, but the entry
   gate in front of it still did.

## Changes

In my-prefect-server:

- **Placement fallback with a bounded wait.** On `resource_unavailable` or
  `placement_error`, the builder tries `hel1`, `fsn1` and `nbg1`, then `cx43`
  and `cpx42` in the same locations. These are all x86 types with at least
  160 GB disk. After a full round of rejections it waits five minutes and tries
  again, for up to 30 minutes, and extends the VM's expiry label by that time.
  Other rejections still fail at once.
- **A dead current slot rolls back instead of deadlocking.** If the serving
  Tantivy slot fails three `/health` probes 20 s apart, automatic continuation
  runs the standard native rollback. The same run then continues with the native
  refresh. A reachable slot that fails validation still fails the run.
  Returning to Tantivy remains an explicit slot operation.

Regression tests cover the 412 fallback, exhausted capacity, non-capacity
rejections, the capacity wait, a dead current slot over real HTTP, and a slot
that recovers between probes.

## What this does and does not guarantee

A missed refresh now degrades to native search within one scheduled run. It no
longer causes an outage that needs an operator. It can still demote Tantivy to
native. The 24-hour coverage window against a 12-hour cadence still leaves no
margin for Tantivy itself. Closing that gap needs either more frequent runs or
proven remote coverage renewal. Both are open decisions, not done.

## Before every serving change

The checklist in the [October 31 retro](2026-10-31-search-expiry-outage.md#before-every-serving-experiment)
still applies. Also ask: if the next scheduled refresh fails, can a later one
still promote before the coverage deadline? With today's cadence the answer is
no. Until it is yes, a missed run means native search.

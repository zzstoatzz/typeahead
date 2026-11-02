# Tantivy slot expired after one missed refresh

Status: production restored on Tantivy slot B at 21:07 on November 2. Times are
UTC. This is the same expiry chain as the
[October 31 outage](2026-10-31-search-expiry-outage.md). The October fixes kept
unscoped search up, but did not stop the chain.

## What happened

| time (Nov) | event |
| --- | --- |
| 1, 18:03 | run `0165b29f` takes the checkpoint for build `b1793556191` and promotes slot A at 19:40. A's coverage ends 24 hours after the checkpoint. |
| 2, 05:59:55 | scheduled run `1b11907a` crashes at submission: Hetzner answered `POST servers` with HTTP 412 `resource_unavailable` for `cpx32` in `hel1`. The generic "flow-run failure" alert reached Discord. Nothing retried the run, and the alert did not say what was at stake. |
| 2, 18:02 | slot A reaches its coverage deadline, stops itself, and Fly gives up after three restarts. |
| 2, 18:02 → 18:21 | the Worker fails over unscoped searches to Bluesky after a 3 s primary timeout. PDS-scoped searches return 503. |
| 2, 18:03 | scheduled run `f83204de` fails in 20 s: a `ReadTimeout` on dead A's `/health`, checked before building a replacement. |
| 2, 18:21 | a rollback routes search to the frozen native Zig engine (Oct 27 snapshot). |
| 2, 19:07 | the manual Bluesky proxy is turned on. Slot B's expired resources are removed and a fresh checkpoint build starts. |
| 2, 21:07 | slot B is promoted with build `b1793646597`. Both aliases, PDS scope and freshness are verified on Tantivy, and the proxy is turned off. |

## Root cause

A Tantivy slot stopped itself 24 hours after its checkpoint. The publisher runs
at 06:00 and 18:00 and a refresh takes about 1.7 hours, so only the 06:00 run
could renew a slot taken at 18:00. **One missed run was an outage.**

Four defects turned one cloud capacity rejection into an outage that also
blocked its own recovery:

1. **Provisioning had one placement.** The builder treated a capacity rejection
   in its single location as final.
2. **Nothing retried or escalated a missed refresh.** The only signal was a
   generic failure line.
3. **A dead serving slot deadlocked refresh.** Automatic continuation checked
   the current slot before building its replacement, so every run failed while
   the route pointed at the dead slot.
4. **The slot shut itself down on a timer.** A slot past its checkpoint coverage
   stopped serving instead of reporting stale.

## How a frozen native engine became the fallback

Tantivy replaced the native Zig engine at the October 30 cutover. The cutover
plan listed "native rollback" as a readiness requirement, and the controller
made a healthy native anchor a gate on every Tantivy prepare, promote and
retire. On October 31 the native refresh resources were deleted, but the native
backend was kept as "the rollback anchor required by the current refresh
controller". From then on the fallback was a frozen October 27 snapshot that
nothing could refresh, kept only because our own gates demanded it. The
October 31 retro and the first response to this outage both treated rolling
back to it as a valid recovery. It was not: it served stale data. It also
diverted effort from replacing the dead Tantivy slot, and its health gate then
broke the promotion that restored Tantivy.

## Changes

- **Builder:** capacity rejections try `hel1`, `fsn1` and `nbg1`, then `cx43`
  and `cpx42`, and retry for up to 30 minutes.
- **Dead slot:** automatic continuation builds and promotes into the inactive
  slot instead of waiting on the current one. The same handling for a slot that
  is serving but past its coverage ships with the new image, on branch
  `drop-native-provenance`.
- **Alerts:** each fires once, on change. They are listed in
  [operations](../operations.md#missed-refresh-alerts).
  - After 13 hours with no completed refresh, Prefect retries the refresh and
    says what is at stake. A probe confirmed it triggers and delivers.
  - At 15 hours of coverage age, the Worker posts "refresh missed" with the
    hours left.
  - The alert spec named a deleted Discord block and would have silenced every
    automation if re-applied. It now resolves blocks by name, and a test rejects
    server UUIDs.
- **Native engine removed:** Worker routing, the controller's rollback, refresh
  and gates, the native Fly apps and their tokens, and the route's native
  fields are all gone.
- **Slot runtime:** it never stops on coverage age. A stale slot keeps serving
  and reports `coverage_stale`. This is on main and built as a new image from
  the unchanged binary. It is not serving yet.

## Open

- **New image qualification.** The new image runs only after a complete
  six-gate release proof. Its first candidate on November 2 passed the in-image
  functional and recovery tests. Server time was about 0.5 ms slower than
  production on three warm-query runs, so the matched-performance gate does not
  pass as written. The deployed scope and live-update checks still need a cloud
  verification run. Until it ships, slot B runs the old image, which still stops
  itself at its coverage deadline. The alerts and retries above stand in for it.
- After the new image serves, the controller drops the native provenance fields
  that the old image still validates (branch `drop-native-provenance`). Then the
  `releases/search/` objects in R2 are deleted.

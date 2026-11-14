# 2026-09-09: the ingester panicked and did not die, again

Same failure as [2026-09-03](2026-09-03-ingester-panic-hang-and-turso-saturation.md),
five days after the fix that was supposed to end it. About 50 minutes of no ingestion, ended by a manual restart; the process would have sat there indefinitely.

## timeline (UTC)

| when | what |
|---|---|
| 04:18:39 | last record Logfire received from the ingester: an `ingest.heartbeat` |
| 04:18:43 | Fly log: panic, trace frame in Zig 0.16 `std/Io/Threaded.zig:1797` (the worker-thread dispatch line, `runnable.startFn`); the panic itself was inside an Io task. Message not retained: Fly's log window had rolled past it by the time this retro was written, and the panic never reached Logfire |
| 04:18:49 | last ingestion progress in Fly logs |
| 04:29:21 | watchdog: `stalled (last event 632s ago, last ok flush 637s ago), exiting for restart`. No exit follows |
| 04:29 → 05:08 | process alive: main thread in `accept`, every other thread parked on a futex. `/health` answers 200 |
| 05:05 | triage begins, prompted by the logfire "ingester heartbeat missing" alert, from `my-prefect-server` (`docs/incidents/2026-09-09-alert-triage.md`) |
| 05:08:22 | machine restarted by hand; resumes from cursor 33458998972 |
| 05:40 | source-event age back under 10 min while draining; no backpressure |

## why 0.3.2 did not fix it

logfire-zig 0.3.2 bounded `flush()` by passing `exit_flush_timeout_ms` down
to each provider's `forceFlush`. otel-zig's `BatchLogRecordProcessor.forceFlush`
(and the span processor's twin) takes that timeout and then:

1. `self.mutex.lockUncancelable(io)` — unbounded;
2. `while (export_in_progress) export_complete.waitUncancelable(io, &mutex)` —
   unbounded, the timeout is not consulted;
3. if another flush is already running, `flush_complete.waitUncancelable` —
   unbounded once `remaining_ms` is non-zero.

So the bound only held when there was nothing to wait for, which is exactly
what the 0.3.2 test checked (`flush is bounded ... with no exporter`). When the
export thread is the one that panicked, `export_in_progress` is true forever:
the panic hook waits on itself in (2), and the watchdog ten minutes later
waits in (3). Same two waits as 09-03, one layer down.

## fix

`services/src/fatal.zig`. Every fatal path (`exitForRestart`, used by both
watchdogs, the firehose exit, handler init and cursor failure; and
`panicHook`, installed as `pub const panic`) arms a deadman first: a raw
`std.Thread` that sleeps on libc `nanosleep` and then `exit(1)` / `abort()`s.
It shares nothing with the Io runtime or the telemetry locks, so it fires
regardless of what the flush does. The flush still runs and still ships the
message when the exporter is healthy; it just cannot hold the process past
5 s (exit) / 3 s (panic). If the deadman thread cannot be spawned the process
terminates immediately instead of flushing unarmed.

Tests: the deadman fires while a flush is still hanging; does not fire early;
production deadlines exceed logfire's own per-provider budgets. What is not
tested: a real wedged export in a real process. The next panic is that test,
and it should show up as a Fly restart with `fatal: telemetry flush did not
finish` on stderr rather than a missing-heartbeat alert.

## still open

- **the panic site.** Two occurrences on the ingester (09-03 `reached
  unreachable code`, 09-09 unknown message) and one on the search box (09-04,
  same `unreachable`). Both times the message scrolled out of Fly's log window
  before anyone saved it. The panic hook prints message + trace to stderr
  first, so the next one is capturable: pull `fly logs` within the hour, or
  route Fly logs somewhere durable.
- **otel-zig / logfire-zig.** `forceFlush` should honor its timeout at the
  lock and both condvar waits (timed waits, or a try-lock with a deadline).
  logfire-zig's "bounded flush" claim is false until then; the deadman above
  is the belt regardless.
- **alerting.** The heartbeat-missing alert is what got this noticed. The
  watchdog now converts the same failure into a Fly restart within about
  11 min on its own; the alert stays as the backstop.

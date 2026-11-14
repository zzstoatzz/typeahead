# 2026-09-03 — ingester alive-but-dead for 22 h; then a viral burst saturated Turso through the worker

## What happened

Two incidents, one day apart, that met in the middle.

1. The ingester panicked at 20:16 UTC on 09-03 and did not die. The panic
   hook and the watchdog both waited, unbounded, on an export thread that
   was the one panicking. The process sat alive and dead for 22 hours;
   nothing new was discovered in that window. Another session restarted the
   machine at 18:14 UTC on 09-04 after the logfire heartbeat alert had been
   firing for 17 hours.
2. An adopter (`bsky38.com`) went viral around 17:15 UTC on 09-04. The engine
   handled 20× volume at a p95 of 12 ms. The worker did not: every empty
   engine answer became a full-corpus Turso FTS query, Turso hit its
   connection limit, and the ingester's `/admin/ingest` writes failed behind
   it until its backlog overflowed and dropped events. Serving was 2xx the
   whole time; discovery was losing data.

Both were invisible from the outside. Health said ready, searches answered.

## Timeline (UTC)

| when | what |
|---|---|
| 09-03 20:16:10 | ingester logs `panic: reached unreachable code`; log output continues ~9 s, then silence. No stack trace |
| 09-03 20:26:52 | watchdog: `stalled (last event 635s ago ...), exiting for restart`. No exit follows |
| 09-04 ~01:00 | logfire alert "ingester heartbeat missing" starts firing (heartbeats = 0) |
| 09-04 17:15 | bsky38.com traffic begins; engine volume 20× by 17:45 |
| 09-04 17:30–19:00 | worker errors: 1,310 × "Database connections limit exceeded", 967 × `turso_fallback_timeout`, 22 × `/admin/ingest` failures |
| 09-04 18:10 | this session reports the burst as "holding" from engine and Fly metrics alone — wrong about upstream |
| 09-04 18:14 | other session restarts the ingester machine; it resumes from cursor 33310380215 |
| 09-04 19:19–19:21 | ingester: `ingest batch failed ... will retry`, then `backlog overflow (4000 ingest), dropping oldest events` |
| 09-04 19:25 | worker deployed with the empty-result Turso fallback off (`5272752`); worker errors go to zero, ingester flushes clean |
| 09-04 19:32 | ingester redeployed on logfire-zig 0.3.2 (bounded flush, stderr-first panic) |
| 09-04 19:43:06 | search box panics with the same `reached unreachable code`; aborts (exit 134), Fly restarts it, ready at 19:43:21. Worker sends every query to Turso for those 15 s: 119 connection-limit errors, 83 timeouts; ingester backlog overflows 19:43:35–19:43:47 |
| 09-04 19:5x | worker deployed with Turso-on-unreachable off too; engine-down now answers empty |

## Root causes

**Hang.** logfire-zig 0.3.1's `flush()` called `forceFlush(null)`, unbounded,
and both exit paths in the ingester ran it: `panicFn` before `defaultPanic`,
and `watchdogLoop` before `std.process.exit`. The batch processor's
`forceFlush` waits for `export_in_progress` to clear. When the export thread
itself panics mid-export, that flag never clears, so the panic hook waits on
itself. The watchdog, ten minutes later, waits on the same flag. Two guards
added on 09-02 to make failures visible turned a crash-and-restart into a
silent hang. Before 09-02 the panic would have killed the process and Fly
would have restarted it within seconds.

**Saturation.** The worker's empty-result fallback re-asked Turso with a
full-corpus FTS query whenever the engine returned nothing. Its reason was a
lagging replica in the search box, which was removed on 09-02; request-
indexing already pushes the row into the engine's overlay synchronously. A
typeahead under viral load produces a stream of unique, mostly-empty
prefixes, so the fallback ran constantly, each call holding a Turso
connection for up to 2.5 s. Turso is single-writer and shared with the
ingester's writes, so the ingester lost the fight.

## What went wrong in the response

- **Declared "holding" from the wrong layer.** Engine latency, Fly CPU, and
  worker 2xx counts all looked fine, and I said nothing upstream was
  straining. The worker's error log and the Turso subrequest count on the
  Cloudflare dashboard said otherwise, and the user found it there. The
  serving invariant is about the request path; Turso is upstream of it and
  needed its own check.
- **The 09-02 observability change was not reviewed for exit paths.** A
  flush on an exit path is a wait; a wait without a bound on a shared
  process is a hang waiting to happen. That is a review question, not a
  production discovery.
- **The fallback outlived its reason by two days.** The replica-removal plan
  removed the replica but not the worker code that existed because of it.
- **Two agents touched prod without coordinating.** The other session
  restarted the machine, correctly, and then told this one. That was the
  right order for an outage; it still deserves a line here.

## Action items

- [x] worker: empty-result Turso fallback is opt-in (`TURSO_FALLBACK_ON_EMPTY=1`), off by default — `5272752`
- [x] logfire-zig 0.3.2: `flush()`/`shutdown()` bounded to 3 s per provider; `panicFn` prints message + stack trace to stderr first, flushes within 1.5 s, aborts; re-entrant panic falls through — `a57fc0e`
- [x] services on 0.3.2 — `5103f96`; ingester redeployed 09-04
- [ ] search box redeploy on 0.3.2 once the burst subsides (a single-machine restart under this load would push every query to the unreachable-engine Turso fallback)
- [x] the unreachable-engine fallback has the same shape as the empty-result one — proven 19:43 UTC when the search box panicked (same `unreachable`, exit 134, back in 15 s) and Turso saturated again for those 15 s. Now opt-in (`TURSO_FALLBACK_ON_UNREACHABLE=1`), default answers empty
- [ ] per-request Turso writes: `recordRequest` + `recordMetric` + `recordTrafficSource` are 2 connections per cache miss. Coalesce per isolate (Durable Object, or a KV-batched counter flushed by the cron) so counters cannot compete with ingest writes
- [ ] find the `unreachable`: the next panic will print its trace. Until then, the site is unknown (not in the ingester's own code; zat 0.4.0's are guarded)
- [ ] dropped events (19:21 UTC window): the relay cursor advanced past them. Recover via the PLC identity job for new DIDs; profiles come back through enrichment. Not a full recovery — record what the next `typeahead-plc-identity` run finds
- [ ] a logfire alert on worker `SQLITE_UNKNOWN` / `turso_fallback_timeout` counts, so Turso saturation pages before the ingester starts dropping

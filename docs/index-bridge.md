# Index bridge

Status: built and tested locally on 2026-11-10 (issue 42). Nothing runs it yet. It is step 3 of the [embedded-store plan](plans/embedded-store-service.md): the code that pushes the store's changes into the Tantivy server on the same machine, replacing the Turso poller in `services/tantivy-input/`.

The code is `services/src/bridge/`: `source.zig` is the interface the store side implements, `wire.zig` is the client for the server's private endpoints, `Bridge.zig` is the loop.

## What it sends

The bridge uses the endpoints the poller uses, unchanged, on the loopback admin listener with `Authorization: Bearer $ADMIN_SECRET`:

| request | body | answer |
|---|---|---|
| `GET /index/status` | | `build_id`, `snapshot_watermark`, `watermark`, `import_complete`, `source_observation_revision`, `moderation.source_observed_at` |
| `POST /admin/source-state` | `{"dids":[…]}`, at most 500 | `source_cursor`, `source_observation_revision`, `actors:[{did, document_sha256 or null}]` |
| `POST /admin/source-page` | `{"after_did":"…","limit":n}` | `dids`, `source_cursor`, `source_observation_revision` |
| `POST /admin/source-reconcile` | `actors`, `expected_documents`, `source_cursor`, `expected_source_cursor`, `expected_observation_revision`, `batch_id`, optional `watermark`, optional `policy_snapshot` | `applied`, `replayed`, `watermark`, `source_cursor`, `source_observation_revision` |

A reconcile is refused with 409 unless the committed cursor equals `expected_source_cursor`, the revision equals `expected_observation_revision`, and every document's hash equals the one `source-state` returned. It commits documents, cursor, watermark and policy together or not at all. `batch_id` is the SHA-256 of the rest of the body, so an identical resend is answered with the stored receipt.

Each document is the actor's current record: `did`, `handle`, `display_name`, `avatar_url`, `labels`, `associated`, `created_at`, `pds`, `quality_score`, `hidden`, the three counts and `version`. A record whose status is not live, or that has no handle, is sent as a tombstone (`deleted: true`, every other field empty). Labels are rendered the way `actors.labels` is projected today: `src`, `uri`, `val`, `cts`, and `cid` and `exp` when present; negated labels are left out. `last_activity_at` is always sent as 0, because the store does not log a change when only that stamp moves.

## The cursor is the record of progress

The server stores an opaque cursor in the same commit as the documents. The bridge puts its change-log position there:

```json
{"schema_version":1,"source_policy":"store-change-log-v1","source_id":"…",
 "build_id":"…","snapshot_watermark":0,"position":3009,"through":1794342502,"cycle":null,
 "reconciliation":{"schema_version":1,"round":2,"phase":"idle","after_did":"",
   "round_started_at":0,"last_completed_at":0,"last_completed_started_at":0}}
```

`position` follows the store's convention: the next change-log sequence to read. Because it commits with the documents it covers, it is the only authority on what the index has applied. The store's own bridge cursor is a copy, written after the index has committed, so the store knows what it may trim.

One push:

1. read change-log entries from `position`;
2. coalesce repeated DIDs and read each one's current record;
3. ask `source-state` for those documents' hashes and the revision;
4. reconcile with the cursor and revision as guards and the new `position` inside the new cursor;
5. hand the acknowledged position to the store.

What a failure at each point leaves behind:

| failure | index | next step |
|---|---|---|
| before the request is sent, or the request never arrives | old cursor | rebuilds the push from current state |
| server dies before its commit | old cursor | rebuilds the push |
| server commits, the answer is lost | new cursor | reads the cursor, copies the position to the store, sends nothing again |
| position write to the store fails | new cursor | same |
| 409 (another writer, or stale knowledge) | unchanged | re-reads cursor and revision, rebuilds |
| 400 (the server refuses the body) | unchanged | halves the batch and retries; progress stops until it is accepted |

A push carries current state, not a recorded event, so repeating or reordering pushes cannot move the index backward, and coalescing loses nothing.

## Ordering and `version`

The server does not order reconciled observations by `version`: a document that differs from the stored one is written, one that differs only in `version` is counted as `replayed` and left alone. Ordering comes from the guards. The bridge sets `version` to the change-log sequence that caused the write, or to its position for a resync write, so the stored value says which entry produced the document. No server change is needed for this.

## Watermark and readiness

The server serves only while its `watermark` is within `--max-sync-lag-s` of now (120 s on a production slot). The bridge sends the wall-clock second at which it began a log read that reached the end of the log, never lower than the server's current value. With nothing to push it sends an empty reconcile every `heartbeat_s` (30 s by default), as the poller's empty final page does today.

No watermark is sent until one full resync round has completed against the index. An index that is being filled, or that is known to have missed changes, therefore goes stale and stops serving instead of reporting fresh.

## How often it pushes

After a step that drained the log and has no resync page to send, the loop rests `poll_interval_ms` (1000 by default; the store role reads it from `INDEX_PUSH_INTERVAL_MS`, 100 to 30000). Changes logged during the rest are read together and go out as one push, so while the bridge keeps up the interval is the most often the index commits, and a change waits at most the interval, plus the push in flight, before the index has it. A step that stopped short of the end of the log is followed at once, so a backlog or a resync is not slowed by the interval. The ceiling is the 30 s heartbeat: a push that reaches the end of the log carries the watermark, and the index stops serving at 120 s without one.

Each push is one index commit: a new segment, and a reload that reopens every segment's readers. The Turso poller it replaces runs once every 40 s.

## Full resync

A round walks every record in the store in DID order and reconciles each page, then walks every DID in the index through `source-page` and tombstones the ones the store has no record of. Progress is in the cursor's `reconciliation`, so a round resumes after a restart. Source pages commit once per page; index pages that need no tombstone are skipped without a commit, and the position reached is committed once per step. A round runs:

- at bootstrap, when the committed cursor is absent;
- when the store answers `.trimmed`: the position fell below the log's floor;
- when the index's position is past the end of the store's log, or the cursor names another `source_id`: the store was replaced;
- on `requestResync()`, or every `resync_interval_s` when that is set.

The first three move the position to the end of the log before walking, and withhold the watermark until the round completes. A cursor written by another controller (the Turso poller's) is refused unless `adopt_foreign_cursor` is set.

## What the owner provides

The interface in `source.zig` maps onto the store module one to one: `changes` and `cursors` from `read.zig`, `actor` as a field copy of `record.Actor`, `walk` as a page of the actor range's keys, `setPosition` as `Store.setCursor(.{ .bridge = … })`. `policy` returns null: the store computes `hidden` itself, and the index filters on that flag.

The owner loop calls `Bridge.step()` repeatedly, or `Bridge.run(&stop)`, from one thread. Every store read happens inside `step` on that thread. `step` blocks on loopback HTTP while the index commits, so it should not run on the thread that owns the writer handle. The intended arrangement is a bridge thread with its own reader handle, refreshed at the start of `cursors` and `changes`, and a `setPosition` that hands the value to the writer to fold into its next commit. `health()` and `requestResync()` may be called from any thread.

`health()` reports the stored position, the index's position, the store's `next_change`, lag in entries, the time of the last committed push, the watermark, counts of pushes, applied, unchanged and rejected documents, conflicts, errors, recovered acknowledgements, resync round and phase, and the last error.

## Records the index cannot hold

The bridge checks each record against the server's admission rules before sending. One that fails (a field over 65,535 bytes, invalid UTF-8, a non-finite score, `associated` that is not a JSON object) is sent as a tombstone and counted in `rejected`, so the index drops the actor rather than keeping a stale profile. A label expiry that is not a strict RFC 3339 timestamp is dropped from the label, which then never expires in the index, matching how the store reads it; the server would otherwise stop serving globally on an expiry it cannot evaluate. A change-log entry whose DID the server would refuse is passed over and counted.

## Where the server's contract does not fit

- **`cutover_source_ready` cannot become true.** `/index/status` requires `source_policy` to be `turso-authoritative-observations-v2`, a policy snapshot, and a completed sweep under a day old. The cursor here has the other fields that gate reads (`schema_version`, `build_id`, `snapshot_watermark`, `through`, `cycle`, `reconciliation`). `ready` and search are unaffected. The slot supervisor's cursor validation and coverage report are Turso-specific in the same way.
- **A policy snapshot is one-way.** Once an index has one, it must be refreshed every 300 s forever. The bridge supports it but the store has no reason to use it.
- **An index must come from a verified import.** The server only reconciles into an index created by `import` with a full-source manifest. An empty manifest works, and a bootstrap resync then fills it 500 documents per commit.
- **A restart used to bring back a replaced document.** See the regression in `services/tantivy/tests/restart_merge.py`; the server now starts pending merges before it accepts writes.

## Tests

`cd services && zig build test` runs the unit tests against a model of the server's guards, including random changes with a kill before or after every request. `just --no-dotenv test-bridge` builds the real server and runs `services/src/bridge/integration.zig` against it: 3,000 synthetic actors from an in-memory store, each kind of change checked through `/search`, the server aborted immediately after and immediately before a commit, 60 rounds of random changes with the bridge killed at a random request, a log trim and a server kill. After each phase a full resync must write no document and leave every document hash unchanged.

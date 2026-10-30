# Source-owned observations

October 29, 2026. This describes the local parallel-engine branch. Deployment and
performance qualification are separate root-owned gates. The preceding
`turso-current-state-cutoff-v1` rollout record remains historical evidence.

## Why timestamps are insufficient

`actors.updated_at` and `tombstones.deleted_at` use Unix seconds. Two writes can
have the same timestamp, including a hide/show pair or a restoration after a
committed deletion. `src/handlers/ingest.ts` also updates `last_activity_at`
without touching `updated_at`; that field is part of the stored actor payload.
Thus a timestamp is neither a unique revision nor a complete change detector.
No ordered source outbox was found in the inspected migrations. Increasing the
lookback cannot establish an order or recover arbitrarily backdated changes.

## Accepted ordering

`sync.py --source-mode observed` uses policy
`turso-authoritative-observations-v2`. Timestamp keysets discover identities only.
For each batch, the adapter obtains `/admin/source-state` hashes, the opaque
cursor and observation revision. It then reads those identities from one fresh,
read-only source statement using primary-key joins. A missing row or cleared
handle means deletion; hidden live rows remain hidden. Retained tombstones do
not override a current live source row in this mode.

`/admin/source-reconcile` receives those current actors, expected document hashes,
the prior cursor/revision, next cursor and a deterministic batch ID. The backend
checks those preconditions and commits actors, an incremented observation
revision and cursor together. The source timestamp may stay equal or move
backward only through this explicitly trusted current-state API. Generic
`/admin/apply` retains its version conflict rules and is disabled after migration.
A stale client cannot overwrite a newer committed observation. The client never
resolves 409 by changing a timestamp, inventing a source version or dropping an
actor.

Identical retries use the same body and batch ID. After ambiguous transport
failure, the client reads the durable cursor/revision before retrying. An exact
matching commit recovers the acknowledgement; a different cursor or revision
fails closed. The backend owns its full-document digest format; the source
adapter treats digests as opaque values.

## Anti-entropy and freshness

The bounded timestamp overlap remains useful for prompt discovery, but complete
backdate recovery uses two independently persisted identity walks:

1. The source DID walk finds source actors absent from the backend, including
   newly inserted actors with arbitrarily old timestamps. Existing backend
   documents are deferred to the other walk to avoid duplicating profile reads.
2. The backend DID walk reads current source state for known identities, covering
   backdated updates, same-version moderation and physical deletions after
   tombstone pruning. Stale indexed terms form a harmless superset; their current
   backend state is rechecked before reading source state.

All source lookups are indexed primary-key searches, at most 500 identities per
page. A batch observes source state after backend-state acquisition. Cursor
`reconciliation` records schema version, round, both identity positions, stream
completion flags, round start, last completed round start and last completed round time. These share the
same atomic source cursor as polling progress. A resumed process continues the
same walks. No corpus walk runs on a public search request.

A sweep is not one multi-statement source snapshot. Concurrent inserts behind a
walk position or later updates to already visited identities may require another
round. A sufficiently stable row is eventually observed even when its timestamp
is older than any bounded lookback. This is eventual reconciliation, not a claim
that every backdated write is immediately visible.

`through` reports completed timestamp discovery, while
`reconciliation.last_completed_at` reports sweep progress. They must not be
presented as the same freshness guarantee. Default two extra pages per cycle is
conservative and has no qualified corpus-wide SLO; measure full-size sweep time,
source egress and write/merge pressure before choosing operational pacing or a
readiness bound. Gaps exceeding seven-day tombstone retention still fail closed;
automatic full-gap recovery is not implemented here.

## Client pacing changes awaiting deployment

The client separates accounted sweep consumption from reserved ordinary polling
capacity within the existing process byte ceiling. Typed sweep exhaustion pauses
bulk work and retains all consumed-byte counters; it neither restarts early nor
grants another quota. Total exhaustion still fails explicitly. Defaults reserve
one quarter of the 512 MiB ceiling, allow two additional pages, time-slice sweep
work at five seconds between pages, and wait one hour after a completed round.
Active rounds preserve durable start/progress. The first committed page after
cooldown records a new start without changing prior completed-round timestamps.

Known source identities require no profile/policy fetch when all backend documents
exist. Those pages commit only identity progress and observation revision, without
policy or polling-watermark refresh. Missing or changed profile observations retain
the same-statement actor/policy path. There is no server API or binary change.
Actual HTTP/SQLite + prebuilt Rust tests cover exhausted sweep allocation followed
by ordinary polling on the same client, policy/watermark preservation, changed-page
policy consistency, mid-page exhaustion without partial progress, explicit total
exhaustion without quota resets, and sliced progress/cooldown across recreated runners.

These client changes are local and are not deployed. Defaults do not meet a
one-day whole-corpus sweep at the current corpus size. Measure source/backend page
time and bytes, change fraction, metadata/segment writes and serving tail latency
on an isolated final-size candidate before faster pacing or a larger approved byte
allowance. Polling lag and completed-round coverage remain independent gates; no
admission ceiling was relaxed.

## Moderation boundary

Turso-backed moderation writes materialize `actors.hidden`; fresh observations
carry that effective flag, including equal-second changes. Native local
`source=1` operator overrides have separate ownership and are not inferred from
Turso. Their actual presence, precedence and migration remain a root-owned gate.
The adapter does not contact an external moderation service or invent decisions.

## Local verification

`test_current_state.py` uses actual HTTP and SQLite to verify primary-key query
plans, same-second hidden/activity mutations, backdated discovery and absence
without a retained tombstone. The extended `test_rust.py` exercises the actual
Rust HTTP API, including migration from a stalled legacy equal-version restore,
moderation, both identity walks, a write between stream phases, restart and
repeat observations without reindexing. Its generated receipt is evidence only
after that exact source/binary pair passes; see the receipt's hashes.

The local October 29 receipt passes against release binary
`43af08cc3c245388f285997781d9e70ff8069da4e87a5108544abbdad40faf59`.
It includes document/revision CAS rejection, changed-body batch-ID rejection,
private-route authorization, discarded acknowledgement recovery, and actual
process aborts before/after the source-owned commit. Documents, revision and
cursor recover together; exact retry receipts survive another restart without
an additional revision. Synthetic payloads and servers are removed after the
run, with core dumps disabled. Earlier transient storage failures coincided
with a full local filesystem; the unchanged binary passed after owned build/test
cache cleanup. This is not a cloud deployment or a full-corpus sweep-time result.


## Checkpoint admission and serving expiry

The optional checkpoint adapter is locally verified, not deployed. It binds the
exact provider receipt (at most 900 seconds of fetch work), materialized SQLite/WAL proof, full stream count
and engine-verified footer hash. The raw checkpoint and independently retained
native rollback anchor have separate counts and identities; a native prefix hash
is never substituted for the raw source. Public import metadata retains policy
hashes/counts, never operator rules.

`verified-checkpoint-import` records the actual completed import without claiming
an additional identity walk. A subsequent source-authoritative CAS binds that
record to the same replay floor and source origin. Admission requires a genuinely
completed remote poll and fresh canonical policy after the provider cut, strict
query settings, complete expiry coverage, and the unchanged handoff hashes. The
reader's traditional `cutover_source_ready` stays false until an actual remote
full round completes. Fixtures remain test-only and cannot qualify production.

Coverage expires one day after the verified provider clock before the cut; the
full export/import pipeline must also complete within one day. Ordinary polls
cannot renew that deadline. Only a genuinely completed remote full round can
renew it. The existing slot supervisor stops both reader and reconciler sixty
seconds before coverage expiry and refuses an expired cached restart. This adds
no search proxy or reader change. It requires an independently qualified refresh
and fallback procedure before production use.

The installer suite passes thirteen tests, including the bounded fetch receipt and actual SQLite/WAL fetch,
canonical stream, strict Rust import, live SQL polling/policy, freeze/install and
HTTP admission. The slot suite passes eight tests with one optional Linux image
test skipped; actual reader/reconciler processes terminate at the coverage
boundary, cached restart fails without another artifact download, ordinary polls
do not renew coverage, and an actual remote full round does. No cloud release or
full-corpus source qualification is inferred from these local fixtures.

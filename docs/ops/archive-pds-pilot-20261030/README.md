# Cloud archive PDS pilot — October 30, 2026

Historical live-write experiment, superseded by the [snapshot join](../archive-pds-candidate-20261030/README.md). Do not resume `backfill.py` or its remaining-work list: the selected approach joins archive metadata during cloud snapshot construction and live reconciliation without a bulk source-database backfill. The measurements below describe the earlier experiment.

The existing archive metadata can fill a substantial portion of Typeahead's
missing PDS fields, but does not cover the entire backlog. No new crawler,
service, archive copy, or HeavyPad work was used.

## Measurements

The read-only archive scan covered 16,300,003 records in 220 seconds. Only 123
have an explicit PDS field. Excluding observed unknown/mirror/relay buckets from
the separate download-host field leaves 16,249,268 candidate mappings. A host is
not necessarily the current authoritative PDS.

A reproducible random sample of 5,000 source rowids yielded 4,511 existing actors:
989 already had PDS, and 3,522 lacked it. Of those missing PDS, 1,773 had candidate
archive mappings (50.3%), 1,739 had no archive row, and 10 had excluded hosts.
This estimates overlap; it is not a complete backlog enumeration.

All 100 randomly selected candidate mappings matched current authoritative DID
documents. The small sample does not prove that every candidate is current.

## Five-account pilot

Five checked candidates were absent from their correct scope before repair.
The existing PDS repair ledger applied a single conditional batch: empty PDS
and unchanged source `updated_at` were required. All five writes returned
confirmed receipts. No handles, ranking fields or other actor data were changed.

Within 38 seconds, all five accounts appeared in correct-scope backend searches
and remained absent from a wrong scope. Subsequent public canonical and legacy
alias queries both returned all five accounts. Searches used
`X-Client: smoke-testing`. Search remained ready, with no ingestion backpressure.

The first attempt used the local read credential and received Turso `BLOCKED`.
All five source rows were reread and confirmed unchanged before a new attempt
used the existing cloud ingester's writer credential. Credentials were passed
in memory, never saved in the audit files. Both attempt records are retained.

## Remaining work

- Enumerate and repair qualified overlap in resumable paced batches.
- Assess remaining missing mappings explicitly; this archive alone cannot be
  claimed to cover the full backlog.
- Verify and fix ongoing identity resolution/retry behavior as necessary.
- Verify final source and serving coverage and reconcile public documentation.

## Evidence

- `measurement.json`: archive totals and source sample counts.
- `overlap-validation.json`: 100 candidate comparisons with DID-document PDS.
- `pilot-receipts.json` and `rejected-attempt-receipts.json`: conditional writes
  and reconciliation of the rejected attempt.
- `pilot-events.jsonl`: before/after health and scoped-search assertions.
- `public-checks.json`: canonical and legacy public endpoint results.

## Expanded stages and freshness fix

The next stage confirmed 1,000 conditional writes. All 1,000 source PDS values
were reread successfully, and 30/30 sampled accounts passed correct/wrong-scope
backend checks. The sequential backfill then scanned 10,000 missing-PDS source
rows and confirmed 4,909 writes; 5,057 had no archive row, 29 had excluded hosts,
and five were hidden. Another 100 authoritative DID-document comparisons all
matched those newly written values. These are separate from the original five
repairs and the earlier 1,000-account stage: 5,914 repairs in this effort so far.

The sequential run checkpoint is source rowid 13,279, with a fixed upper rowid
14,326,909. The operation resumes with conditional empty-PDS writes and refuses
to resume past ambiguous or unfinished ledger attempts. It uses the existing
repair ledger, with a qualified maximum batch size of 1,000 and atomic batch
preparation. Nine ledger tests pass, including concurrent changes, failed batch
preparation, and lost response after commit; all 33 PDS tooling tests pass.

The one-off operation is `backfill.py` here. Its owner is this Typeahead repair;
it runs on the existing Stream cloud host, reads `/data/stream/meta.rocksdb`
read-only, and stores checkpoints/receipts under `/tmp/typeahead-archive-backfill`.
The scratch budget is 4 GiB. Each invocation bounds time and successful writes;
`STOP` stops after the current page, and a file lock prevents concurrent writers.
Credentials arrive only through stdin. The prebuilt rocksdict 0.3.29 reader is
under `/tmp/typeahead-overlap-reader`. Preserve small final receipts and remove
these temporary paths when the repair is complete. No archive payload is copied.

The deployed identity fix adds existing 60-second/15-minute retries after a
failed DID-document lookup, keeping the bare-DID discovery behavior and avoiding
writes of unknown identity fields. Native service tests passed (180 tests,
six skipped). Production ingester machine `7845636a3d0ee8` now runs image
`sha256:784caa98f4a1e0d68a8f39f3852e35dd94c99e09fa52e4d40fb89b5994d45cc0`.
The restart is replaying buffered relay traffic. Bulk repair waits for source
events less than 30 seconds old, recent successful ingestion, no backpressure,
and a ready search index with bounded sync lag. The retry fix is deployed;
complete freshness qualification and the full backfill are not yet claimed.

README review: the architecture, API and deployment entry points are unchanged;
the retry behavior belongs in `docs/discovery-and-identity.md`. No README edit
was needed for this stage. Public `/docs` cleanup remains pending.


## Documentation preparation

Commit `f75bba8` simplifies the public API page and removes the stale low-volume
smoke-test instruction from `/llms.txt`. This is prepared locally, not deployed.
Type checking and lint passed; the Worker suite passed 127 tests with one skipped.
The page was inspected at desktop and phone widths in light and dark themes;
navigation, expandable details and theme switching worked. The final phone check
had no page-level horizontal overflow or console errors (code blocks scroll).
The temporary local preview server and browser tab were closed after inspection.
Publication remains pending the repair outcome and final documentation review.


## Resumed backfill check

The initial wait-only invocation exhausted its health-wait limit without new
writes. After relay replay caught up, the next bounded invocation resumed the
same checkpoint. A random sample of 30 settled repairs from its first 1,000
confirmed writes passed source-value, current authoritative DID-document,
correct-scope and wrong-scope checks. See `run-2-checks.json` and
`verify-run-2.py`. This is an additional sample, not full-backlog verification.

The real-SQLite ingest regression now also checks that a PDS migration replaces
the old host while a later bare-DID or empty-PDS profile event preserves it.
All five ingest tests, type checking and lint passed. Production resolution
failures are not injected into real users' identities for this test.

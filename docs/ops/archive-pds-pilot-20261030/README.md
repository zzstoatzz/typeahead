# Cloud archive PDS pilot — October 30, 2026

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

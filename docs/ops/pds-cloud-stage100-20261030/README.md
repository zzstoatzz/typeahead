# Cloud PDS repair: 100-account stage

October 30, 2026. Dedicated Fly app `typeahead-pds-repair`, machine
`d897350c349e98`, one shared CPU and 256 MiB RAM. The machine was destroyed
following receipt collection. Production allocation and routing were unchanged.

- Discovery selected 100 missing-PDS candidates from 105 examined source rows.
- Dry-run proposed 99 repairs. The malformed `did:plc:test123` was unresolved
  and untouched; subsequent discovery rejects malformed PLC identifiers.
- All 99 conditional writes were confirmed and source-read back; write phase
  took 112.22 seconds. Handles were unchanged.
- Original public verification passed 43 identities, then stopped on HTTP 503.
- Read-only resumption verified the remaining 56 in 512.46 seconds, checking
  source plus direct/backend and public global, correct-PDS and wrong-PDS cases.
  There were no HTTP retries or mismatches during resumption. No writes repeated.
- The union of verification receipts equals the 99 confirmed write DIDs exactly.

[Event receipts](events.json) preserve the original run and resumed verification.
The full queue/ledger receipt archive is 41,070 bytes, retained locally in
`output/pds-cloud-pilot/stage100.tgz`. This archive contains receipts, not a corpus
snapshot. The original process exited on a 503, so it has no completion summary;
its partial request totals must not be presented as complete traffic accounting.

This qualifies expansion to the bounded 1,000 stage. It does not qualify the full
backlog or demonstrate a recurring freshness deployment. That stage uses ten-row
conditional batches, a shared 64 MiB response-body budget, a 3,600-second runtime
cap, a 1 GiB persistent cloud volume, and the same CPU/RAM size. It verifies every
confirmed identity directly and samples public verification as specified in the
[repair contract](../../pds-identity-repair.md).

# PDS identity repair tooling

Status: October 20, 2026. Full coverage repair and recurring coverage are
unfinished. [Cleanup](pds-repair-cleanup-20261020.md) removed the abandoned
controllers and unconnected framework; execution is stopped. Two guarded source repairs were verified in native and public
search. Offline reconstruction and larger-cutoff experiments are not deployed
coverage. Production still uses the 10,000-actor cutoff.

The weekly identity path defaults to missing handles, omitting actors that
already have handles but lack PDS values. Simply changing its selection would
also expose an older writer that overwrites existing identity fields. The tools
here separate discovery, authoritative reconstruction, conditional writes and
verification.

## Data preparation

`pds_coverage.py` prepares candidates from an existing export or snapshot.
Snapshot candidates require fresh source reads; `pds_candidate_sidecar.py`
accounts for omitted nonsearchable candidates with bound source provenance.
`pds_plc_reconstruct.py`, `pds_plc_tail.py` and `pds_plc_merge.py` reconstruct
history and validate tail continuity. Their output is not approval to apply.
`pds_reconstruction_report.py` accounts for unresolved and excluded identities.

The abandoned full-export downloader and duplicate cloud reconstruction controller
were removed during cleanup. No further bulk downloads or repair jobs are enabled.
Retained HeavyPad results, cloud snapshot/candidate data, and the completed PLC
tail remain evidence, not approval to apply. `pds_bundle_download.py` remains a
dependency of the retained tail capture's durable transfer budget.

## Applying and verifying repairs

`pds_repair_canary.py` permits at most five identities, with fresh authority and
source reads, a completed health baseline, conditional writes and receipts.
`pds_repair_ledger.py` records prepared, sent, confirmed and ambiguous attempts;
unknown writes must be reconciled, never blindly retried. Updates only fill an
empty PDS when the observed source version still matches. `Ledger.retry`
requires the current attempt to have a definite `confirmed` or `conflict`
outcome and retains its original receipt in `attempt_history`. Only one current
attempt per DID is allowed. Ambiguous outcomes remain ineligible even after
reconciliation reports `unchanged` or `present_unattributed`; these do not prove
the old request cannot still commit. Explicit retries still require fresh
source and authority reads plus normal admission. No recurring writer is enabled.

`pds_fly_progress.py` binds successful completed sync logs to the expected
uncordoned serving generation. `pds_repair_verify.py` checks native canary
propagation. The unconnected generic stage/admission wrappers and undeployed
recurring discovery, reconciliation and retry queue were removed. Git retains
the experiments if their evidence is needed; no recurring repair was shipped.

## Verification

Run `python3 -m unittest discover -s scripts -p 'test_pds_*.py'`.
The suite uses real temporary SQLite databases and loopback HTTP servers. It
covers retained transfer budgets, interrupted receipts, conditional conflicts,
deletions, identity changes, PLC tail continuity and health rejection. These
checks do not substitute for production rollout, recovery or throughput gates.

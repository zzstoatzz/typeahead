# PDS identity repair tooling

Status: October 20, 2026. Full coverage repair and recurring coverage are
unfinished. Two guarded source repairs were verified in native and public
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

`pds_bundle_download.py`, `pds_cloud_reconstruct.py` and
`pds_checkpoint_download.py` bound transfers and retain incomplete attempts.
Do bulk transfers on the designated cloud host. HeavyPad is on the metered home
connection; reuse its retained data only for offline qualification. Keep the
source checkpoint distinct from a search snapshot, which omits source rows.

## Applying and verifying repairs

`pds_repair_canary.py` permits at most five identities, with fresh authority and
source reads, a completed health baseline, conditional writes and receipts.
`pds_repair_ledger.py` records prepared, sent, confirmed and ambiguous attempts;
unknown writes must be reconciled, never blindly retried. Updates only fill an
empty PDS when the observed source version still matches.

`pds_repair_stage.py` provides bounded, paced batches but has no enabled
production transport. Its admission callback must independently establish
current authority, writer coordination, live ingestion/search health, eligible
scope policy, storage reserve and rollback readiness. `pds_repair_admission.py`
connects cloud-host, storage and health checks; it does not replace those other
gates. `pds_fly_progress.py` binds successful completed sync logs to the expected
uncordoned serving generation. Refresh that explicit identity only after a
verified cutover. `pds_repair_verify.py` checks native canary propagation.

## Recurring coverage

`pds_identity_discovery.py` captures bounded source-update pages and commits the
cloud-local queue and cursor together. `pds_identity_queue.py` persists retry
schedules and leases without changing the handle job's shared timer. Overlapping,
delayed windows do not guarantee discovery of backdated writes; independent
missing-PDS reconciliation remains necessary. Lease expiry authorizes rechecking
identity, not replaying an ambiguous source mutation. No recurring writer or
schedule is enabled by these modules.

## Verification

Run `python3 -m unittest discover -s scripts -p 'test_pds_*.py'`.
The suite uses real temporary SQLite databases and loopback HTTP servers. It
covers partial transfers, interrupted receipts, conditional conflicts, deletions,
identity changes, durable restart, pagination and fail-closed admission. These
checks do not substitute for production rollout, recovery or throughput gates.

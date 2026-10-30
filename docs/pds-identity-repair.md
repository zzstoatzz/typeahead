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

## Cloud pilot and freshness plan

Status: proposed; no pilot or replacement schedule is deployed. The obsolete home
flow, deployment definitions, bulk writer and installer are removed from source.
Historical run records and retained payloads are not deleted by this cleanup.

1. Run a manual, bounded cloud job using the existing cloud Prefect execution
   environment and an independent scratch directory. No HeavyPad execution,
   relay through home, corpus copy, or PLC bundle download for the pilot.
2. Start with five known missing-PDS accounts: bsky.app, safety.bsky.app,
   status.bsky.app, jay.bsky.team and emilyliu.me. Read fresh source rows and
   authoritative DID documents. Dry-run the proposed changes first, recording
   source version, old value, authority and timestamp. Reject ambiguous or
   invalid endpoints; leave handles and profile fields alone.
3. Apply only those five empty-PDS repairs with compare-and-set against the
   observed source version/value. Retry reads with backoff; reconcile uncertain
   writes before retrying. Limit the pilot to ten minutes and a few MiB of
   receipts; cancel on elevated source or serving errors. Existing conditional
   repair tooling must pass its behavioral tests and cloud admission first.
4. Verify each source write reaches the deployed Tantivy generation and the
   public API. Each exact handle must remain globally visible, appear under its
   authoritative PDS, and disappear under an unrelated PDS. Include unchanged
   controls and one isolated migration/conflict test. Record propagation time,
   request counts, errors, source read/write work, and cost; remove scratch data.
5. Review the pilot before expanding to 100, then 1,000 identities. Use a
   resumable keyset scan in cloud for discovery, not repeated full-table missing
   PDS queries or interactive request-time resolution. Do not extrapolate a
   five-account run into a full-backlog completion estimate.
6. Choose bulk execution from measured costs: bounded direct DID resolution for
   initial batches; cloud-local, resumable PLC reconstruction only if the pilot
   economics justify it for millions of records. Check existing usable cloud
   artifacts before downloading anything. Every bulk phase needs an explicit
   storage/transfer budget and cleanup owner. Validate freshness before applying
   historical declarations. did:web requires its own authority resolution path.

Keep the live identity-event resolver as the fast path. Add a durable cloud
repair/reconciliation queue for newly discovered identities with unknown PDSs,
failed identity resolutions, and bounded periodic revalidation of known PDSs.
Track last successful authority check separately from profile updated_at; profile
refresh does not prove hosting freshness. A later migration may replace a
nonempty PDS only with fresh authority and a source-version precondition.
Transient resolution failure must not clear a previously known PDS.

Measure unknown/invalid coverage, queue age, successful verification age, migration
propagation and error/retry rates. Preserve explicit failures and unresolved
records in the denominator. Neither a snapshot rebuild nor source reconciliation
can discover identity data absent from the source.

The five-account pilot is the next implementation step after this cleanup and
plan review. Full-backlog execution is gated on pilot results, not scheduled here.

### Five-account runner

`scripts/pds_cloud_pilot.py` is the dedicated Fly pilot entrypoint. It refuses
execution outside the `typeahead-pds-repair` app, requires an explicit managed
Tantivy backend, and defaults to dry-run. Each invocation needs a fresh output
directory containing its durable ledger and event receipts. `--apply` enables only
the five fixed missing-PDS witnesses; an ambiguous write stops the run after
readback and cannot be blindly retried. The cloud machine must retain receipts
until they are collected, then be destroyed. No scheduler is installed by this
script. The previous native canary remains historical tooling, not this runner.

Local verification: `uv run python -m unittest discover -s scripts -p 'test_pds_*.py'`.
The added tests exercise real SQLite writes, concurrent profile/hosting changes,
stale authority, subject mismatch, and a committed write with a lost response.

### Freshness queue primitive

`scripts/pds_freshness_queue.py` provides the local SQLite state intended for a
persistent cloud volume. Discovery checkpoints and identity inserts commit
atomically. Verification age is independent of source profile timestamps.
Read leases expire and carry unique ownership tokens; stale workers cannot start
writes or mark a reclaimed identity verified. Writing/uncertain states never
expire into automatic retry. Failed authority reads preserve the last verified
PDS and use bounded backoff. This queue is tested but not yet wired to a deployed
recurring worker; it is not evidence that backlog repair or freshness is complete.

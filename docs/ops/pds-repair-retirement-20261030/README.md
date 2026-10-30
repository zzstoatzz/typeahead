# Standalone PDS repair experiment retired

October 30, 2026. Stopped at the user's request to remove the newly built repair
infrastructure and investigate existing network archive/Hubble data first.

The running verification process was terminated before receipt collection.
The 1,000-account stage had confirmed all 1,000 conditional writes and recorded
286 passing search checks when stopped; it did not finish verification. The
previous five-account and 99-account stages completed verification. No confirmed
source changes were rolled back, and production routing, serving allocation and
live identity ingestion were not changed by this cleanup.

Removed the new cloud pilot/batch/verifier scripts, freshness queue/discovery/
worker/runner, their tests, and the uncommitted sequence-mirror experiment.
Restored the three modified pre-existing canary/ledger/test files to their
pre-experiment versions at `8f9974c`. The earlier home-job removal and deployment
configuration cleanup are preserved. No recurring schedule was installed.

Before deleting cloud resources, retained a 361,852-byte receipt archive containing
ledgers, queue state and logs; the raw export page was excluded. Its SHA-256 and
local path are recorded in [receipt-summary.json](receipt-summary.json).
The retained archive contains final-stage write receipts and partial verification. Earlier evidence remains in the
[five-account](../pds-cloud-pilot-20261030/README.md) and
[99-account](../pds-cloud-stage100-20261030/README.md) records.

Cloud cleanup: the dedicated `typeahead-pds-repair` app, machine
`802e79b630e428` and volume `vol_491j0j10p9dljq9r` were deleted after
receipt retention. The provider confirmed app destruction; subsequent inventory
showed the app absent and its volume listing returned app-not-found.

# PDS identity coverage

Production uses Tantivy without the old native 10,000-actor PDS cutoff. Missing
`actors.pds` values in Turso still cause accounts to be absent from scoped search.
The existing live identity-event path remains in service.

## Repair status

The October 30 standalone repair experiment was stopped and its new cloud
runners, freshness queue and PLC mirror experiment removed. No recurring repair
schedule was installed. The retired home PLC job remains removed.

Confirmed source repairs were preserved: five pilot accounts, 99 accounts in the
next stage, and 1,000 in the final stage. Search verification completed for the
first two stages; the final stage stopped after 286 passing identity checks and
must not be described as fully verified. Receipts and cleanup details are in the
[retirement record](ops/pds-repair-retirement-20261030/README.md).

Before selecting another backfill implementation, inspect the existing network
archive and Hubble for reusable current identity data. Hubble documents a PDS
field on its per-account `getRepoInfo`; bulk DID-to-PDS availability is not yet
verified. Do not infer that repository archives themselves contain hosting
identity or launch another PLC reconstruction on that assumption.

## Retained historical tooling

The older `pds_coverage.py`, candidate sidecar, PLC reconstruction/tail helpers,
and manual repair canary predate this experiment. They are retained historical
tools, not an active pipeline. The canary/ledger retain their original
empty-PDS-only conditional-write behavior and ambiguous-write safeguards.
Neither historical results nor saved archives authorize their execution.

Run their behavioral checks with:

```sh
uv run python -m unittest discover -s scripts -p 'test_pds_*.py'
```

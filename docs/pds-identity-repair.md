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

## Hubble export approach

Hubble stores per-DID PDS and identity-resolution time in its repo identity
metadata (`DbRepoIdentity` at source revision `52960bf`). Its
[public API](https://web.hubble.microcosm.blue/api/) exposes PDS through
`getRepoInfo`, but `listRepos` omits it. The live API was checked; no published
bulk identity export was found in the API docs or current server routes. Export
availability therefore requires an operator response, not another crawler.

Our existing Stream instance reports 16.3 million repos. Its metadata contains
`host` and `pds`, but normal bootstrap can leave `pds` empty and store the final
download host separately. Archive identity events contain no PDS. This is not
proof of complete, fresh hosting coverage and does not justify replaying records.

Next dependency: request a metadata-only `did, pds, resolved_at` export from
Hubble, preferably with account status and an export timestamp. No message has
been sent. Once available, inspect provenance/freshness, validate a small sample,
then conditionally fill missing source values in cloud and verify scoped search.
Use individual resolution only for a measured residual. No new mirror or generic
freshness framework is planned; preserve the existing live identity-event path.

Completion also includes reconciling relevant operations/API documentation and
then simplifying the public `/docs` page (`src/pages/docs.ts`): concise quickstart,
verified parameter/response guidance, fewer duplicated explanations, and a cleaner
layout. Keep `/llms.txt` and README consistent with the final deployed behavior.
That public-page cleanup remains pending until the repair outcome is known.

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

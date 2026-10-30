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

## Existing Hubble interfaces

Hubble's public `blue.microcosm.hubble.getRepoInfo?did=...` returns its stored PDS
metadata for one DID. The handler reads existing repo state; it does not download
repository contents or perform a fresh DID resolution for each API request.
`com.atproto.sync.listRepos` enumerates DIDs but does not include PDS. See the
[API documentation](https://web.hubble.microcosm.blue/api/) and
[handler source](https://tangled.org/microcosm.blue/hubble/blob/52960bf/hubble/src/serve/get_repo_info.rs).

A bulk identity export was proposed without verifying that one is available;
that proposal is withdrawn as a required dependency. No operator request was sent.
The repository's `hubble-mini export-pairs` command exports captured DID/host
pairs from a local mini database; it is not a public full-Hubble identity export.
Its older non-Bluesky snapshot scope and lack of identity verification matter.

Our existing Stream instance reports 16.3 million repos. Its metadata contains
`host` and `pds`, but normal bootstrap can leave `pds` empty and store the final
download host separately. Archive identity events contain no PDS. Assess the
existing metadata and Hubble's existing stored-metadata API before choosing an
implementation. Cached lookups still require one request per DID; bulk viability
and acceptable request rates have not been measured. No new data-collection
system or replacement freshness framework is authorized by these findings.

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

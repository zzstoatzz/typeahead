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

The existing cloud Stream archive was measured read-only on October 30:
16,300,003 repo rows, with only 123 explicit `pds` values. The separate download
`host` field supplies 16,249,268 candidate mappings after excluding observed
unknown, mirror and relay buckets; those candidates are not automatically
current authoritative identities.

A uniform source-rowid sample found usable archive hosts for 1,773 of 3,522
Typeahead actors missing PDS (50.3%); 1,739 were absent from the archive and 10
had excluded hosts. All 100 randomly selected candidates checked against current
DID documents matched. This is sampled overlap, not a full-backlog count.

Five authoritative-checked candidates were conditionally repaired through the
existing ledger, preserving nonempty PDS values and concurrent source changes.
All five became visible in their correct scope and remained absent from a wrong
scope within 38 seconds. The next 1,000 source writes were verified, with 30/30 sampled scoped-search
checks passing. A checkpointed source-rowid scan then repaired another 4,909;
100/100 of those sampled mappings matched current DID documents. The full
backfill remains in progress. Failed DID-document lookups now use the existing
bounded retry path (deployed October 30); retries do not guarantee recovery from
a permanent resolution failure. See [pilot evidence](ops/archive-pds-pilot-20261030/README.md).

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

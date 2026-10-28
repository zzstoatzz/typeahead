# Invalid handle claims in live search

2026-10-28 — confirmed live data-integrity issue; not repaired.

One bounded GET to the public canonical endpoint with `q=donald.trump&limit=5`
returned five DIDs advertising `donald.trump.com`. The first three were exactly
the retained sample DIDs whose PLC claims were checked earlier:

- `did:plc:2222wwgiwub7vgzujm4oi7xv`
- `did:plc:2224jxoasn2qedlvkpylnhvb`
- `did:plc:2224urouejo7v3akjpufobhb`

Their claimed handle DNS and PDS were NXDOMAIN, and Bluesky returned no profiles
for them. See the [authority checks](../evals/handle-sidecar-20261028/identity-followup.md).
This is not confined to the private experiment.

A separate Turso read used `WHERE handle = ? COLLATE NOCASE LIMIT 5`.
EXPLAIN confirmed `SEARCH actors USING INDEX idx_actors_handle (handle=?)`;
the query reported five rows read. All five stored rows had `hidden=0`,
`quality_score=0`, `identity_checked_at=0`, empty PDS and June 15 update timestamps.
The first stored DID was `did:plc:moatpkdbgsu65fc7xsl5rbmg`.
These rows are in the live source database and eligible to be searched.

An exact matching-handle count had a covering-index SEARCH plan but timed out at
the client's 15-second read deadline. It was not retried. The total affected
count remains unknown; an index SEARCH does not make a very large matching range
cheap. Further counting and broad classification belong on an independent copy.
A route-metadata GET returned HTTP 403; no route change was attempted.

## Admission paths

The earlier emphasis on PLC bulk admission was incomplete:

- `scripts/plc-identity-sync.py` writes the first PLC `alsoKnownAs` handle without
  resolving the handle back to its DID.
- `services/src/ingest.zig::handleIdentity` also places the relay event's optional
  handle directly on the protected write queue, then independently offers the
  DID to the verifying identity pool. The pool's existence does not gate that
  initial claim write. This describes repository code, not a new readback of the
  deployed ingester binary.
- `src/handlers/ingest.ts` accepts that supplied handle and overwrites the stored
  handle. It does not itself establish handle ownership.

The sampled rows' `identity_checked_at=0` does not establish which historical
writer created them. The PLC path alone must not be asserted as their provenance.

## Remediation scope

Prevent unverified claims from becoming searchable handles on every admission
path, including the raw event queue and bulk jobs. Retain bare DID discovery.
Contain known invalid associations without a corpus-scale Worker backfill, then
measure and revalidate existing rows offline. Do not conflate an invalid handle
with proof that every corresponding DID/account should be deleted.

The existing domain-block admin operation launches an existing-row backfill;
it should not be invoked casually as a low-cost emergency filter for a massive
match set. A bounded response filter would provide a different containment path
but requires implementation and smoke-gated deployment.

No writes, domain blocks, deletions, deployments or background monitors were
started. No heavypad access or corpus transfer occurred. The live service remains
affected at this observation; the earlier experiment cleanup does not mitigate it.

# How PDS filtering changed

Historical account through October 31, 2026. For the serving contract, see
[PDS identity coverage](../pds-identity-repair.md). This records the progression
from incomplete community search, through a small-host implementation, to
Tantivy filtering supplemented by archive metadata.

## The original problem: finding people within a community

The request was to search accounts hosted on a particular PDS, including people
with custom-domain handles. A handle suffix cannot establish where an account
is hosted. Nor can filtering the global top results recover community members
that were discarded before the filter ran.

The native engine used capped, precomputed prefix lists. Reliable community
search therefore needed its own candidate selection, not just a filter on the
returned list. Missing hosting identity was a separate problem: even a correct
filter could not match accounts whose PDS we did not know. These are distinct
reasons a search could appear broken.

The [October 8 experiment](../evals/pds-scope.md) documents this limitation and
includes a fixture where a community had no members in the global top results
for `bob`, yet the scoped index recovered them. That is evidence of the candidate
selection problem, not proof that a deployed implementation always post-filtered.

## October 20: working scopes, restricted to small PDSs

The native builder added per-PDS prefix lists, selected before merging, ranking
and result limits. This extended the existing bounded-list design at the cost
of additional index entries and update work. The rollout deliberately bounded
eligible hosts rather than claiming large-host capacity from small fixtures.

The default cutoff was **at most 10,000 known source rows per PDS at build time**,
including hidden and handle-less actors. It was not a census of all actual PDS
residents, a 10,000-result limit, or a continuously enforced membership quota.
A known host above the cutoff returned `503 PdsScopeUnavailable`; it was not
supposed to silently return global results. Eligibility was recomputed on rebuild.

Scoped cache keys, backend acknowledgments, identity migration, moderation,
deletions and replay readiness all mattered. The public filter shipped October
20 after isolated and full-corpus checks. See [release evidence](../evals/pds-homepage-20261020.md)
and implementation commit `27cb248` (the scoped compact-index integration).

This made the small-host case usable. It did not solve large-host support or
missing PDS identities, and the native engine retained its broader capped-list
recall limitations.

## October 30: Tantivy removes the host-size cutoff

Tantivy replaced the custom prefix-list retrieval path with complete postings
and filtering before final selection. It can search any recorded canonical PDS
without the native host-size eligibility rule. The engine implementation landed
in `34bd0c5`; `ef32609` records the production cutover and its verification.
See the [cutover record](../plans/tantivy-cutover-readiness.md).

This fixed the retrieval constraint, not the metadata gap. An account with no
indexed hosting identity still could not appear in a PDS-scoped result. Large
PDS support therefore did not mean complete membership coverage.

## October 30–31: use metadata already present in the network archive

Early repair attempts pursued per-account lookups and writes into the live
source database. They were stopped. The [repair history](pds-live-write-repair.md)
records those attempts and their cleanup; they are not a pipeline to resume.

The useful bulk source was the existing Stream network archive. Its cloud
metadata database recorded the host from which each repository was downloaded.
We exported DID-to-host pairs into a versioned SQLite mapping in R2. This reads
archive metadata, not repository contents, and avoids millions of repair writes
through the live Turso database.

The snapshot builder joins that mapping only where source PDS is empty. The
live reconciler uses the same checksum-bound mapping, so profile updates do not
erase the joined value; an explicit source PDS takes precedence. Pilot tests
covered scope, profile changes, PDS migration, deletion and restart before the
full candidate. `ea133d5` records the pilot; `84fa474` wires the mapping through
publication, installation and live reconciliation.

The [full candidate measurements](../ops/archive-pds-candidate-20261030/README.md)
show the change on the same build input:

| Measurement | Before join | After join |
| --- | ---: | ---: |
| Live actors with usable PDS | 2,946,538 | 7,972,692 |
| Share of 12,890,252 live actors | 22.86% | 61.85% |

The join supplied **5,026,154** missing PDS values. **4,917,558** live actors still
had no mapping. The export contained 16,249,268 usable archive pairs, but that
is not the count of Typeahead accounts recovered. The joined candidate entered
production October 31; the subsequent full refresh also preserved the join.
See [recovery and refresh verification](../ops/production-recovery-20261031/).

## What remains unresolved

Filtering now has no per-host actor-count cutoff, but coverage is incomplete.
The archive download host is an observation, not a fresh authoritative DID
resolution; accounts can migrate. Sampled DID-document checks matched, but do
not establish correctness for every exported row.

The mapping is a pinned export, not a continuous subscription to the archive.
Rebuilding an index with the same pin does not refresh the archive observations.
Live authoritative source identities take precedence, but maintaining complete,
current hosting data remains a separate concern from query correctness.

PDS preference is a separate ranking experiment. It is not necessary for the
hard filtering contract and is not a conclusion of this work.

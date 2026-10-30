# Recall fix design

2026-10-28. Investigation and isolated prototypes only. No production changes,
qualification, release or corpus transfer are part of this work.

## Diagnosis and scope

The reported `Justin Fa` miss is consistent with intersecting independently
truncated prefix lists. Matching actors need not rank highly for either word
alone. The 990-actor native fixture proves this mechanism and reproduces the
same empty response for reversed words and inside an eligible PDS. Reprojecting
the matching actors into the overlay makes them visible; a fresh snapshot from
the same effective actor state loses them again. Neither a larger seed nor a
blind rebuild is a durable correction.

There are separate losses after retrieval: the engine selects 50 candidates,
then removes hidden/deleted actors and checks long prefixes. It does not fill
from the surviving tail. A correct fix must address both membership completeness
and eligibility before final selection. CJK/mixed-script grouping and the
silent eight-token query truncation are independent semantic defects; increasing
posting caps cannot fix them. See the [native oracle cases](README.md).

The current source contains these mechanisms before the October performance
work. Both previously captured managed backends reproduce the reported query.
That supports attributing this example to an existing design limitation, rather
than the admission/containment Worker change. It does not establish when the
first affected production request occurred or how many accounts are affected.
No corpus-wide prevalence estimate is available.

## Recommended direction

Retain the immutable offline generation plus live update plane, but replace
independently capped membership as the source of truth for conjunctions.
Keep existing precomputed top lists as an optional accelerator, with an exact
complete retrieval path when they cannot prove the answer. That requires
completeness/rank-bound metadata and a coherent delta, not guessing completeness
from the number of candidates returned.

A candidate design has a term dictionary, complete compressed integer-ID
postings, coherent current eligibility/rank state, and separately stored
profiles. Seek/intersect IDs before fetching profiles; apply PDS, tombstones,
moderation and the entire lexical predicate before final top-K. A score-ordered
representation can stop once enough eligible results are established, instead
of materializing and sorting every broad match. Sparse lists, bitmap containers,
lexicon ranges and per-block upper bounds are alternatives to measure, not
interchangeable performance guarantees.

Do not implement this by removing caps from the existing DID-string lists.
The full-list native control is useful for correctness, but eagerly decodes
memberships into hash maps and sorts the result. It retains substantial request
work and timestamp/prefix write amplification. A production format must also remove the existing u16 per-list count ceiling;
the 65,535 fixture override is not complete for a common prefix with millions
of members. Nor should every arbitrarily
long prefix necessarily become another durable key; a full-term dictionary and
range-aware postings may represent those constraints more economically.

Generation-local rank IDs require a coherent delta: suppress obsolete base
versions, merge current renamed/rescored/new actors, and retain deletion and
source-version barriers across compaction. Ranking bounds must account for
higher overlay scores. Names, scope, rank and moderation must come from one
consistent request view. Never mix IDs, profiles or readiness metadata from
different generations. A rebuild must preserve effective answers after the
replay is compacted away.

An FST can compact the dictionary and enumerate edit-distance alternatives for
handles. That does not recover memberships already discarded by per-prefix
caps, implement arbitrary-order name conjunction, or decide current eligibility.
For typo tolerance, define exact-first ranking, supported edits/normalization,
minimum query length, term-expansion and candidate-work budgets explicitly.
Expanded terms must reach complete memberships. Budget exhaustion needs a
separate internal outcome; an exhausted search is not proof of no match.
Typos are a separate feature gate, not a prerequisite for repairing this miss.
The earlier [full-corpus handle sidecar experiment](../handle-sidecar-20261028/README.md)
found substantially lower typo-query working memory for its FST layout, but
neither variant met the sustained mixed-write qualification gate. Those results
support investigating FST lookup, not claiming it repairs the backend storage
issue or display-name recall.

## Why the simple alternatives are insufficient

- Larger per-key caps move the failure boundary. Adversarial overlap can fall
  below every selected cap. Union-top-N-then-verify and bounded rarest-seed
  retrieval also miss joint matches. Phrase-only and joined-handle fallbacks
  miss reversed words or display-only actors.
- Batch refill helps post-filter starvation while the matching tail is stored.
  It cannot reconstruct omitted membership. A 200-candidate comparator is not
  a complete fix or a bound on posting decoding and overlay intersection work.
- Complete membership followed by full sorting fixes the ASCII retrieval
  controls, but does not establish acceptable request cost at production scale.
- A generic FTS5 MATCH followed by authority sorting is semantically plausible
  but can still walk all broad matches. Returning to FTS5 needs a materially
  different, measured plan rather than a warm small-data success.
- Tantivy and a bespoke Zig index are the most relevant next native candidates
  for comparison. ClickHouse, Lucene/OpenSearch and Vespa are broader architecture
  comparators, not established winners or a recommendation for a JVM migration. No experiment here compares those engines
  at equivalent scale, ranking, update load and latency. Retain regional local
  serving and a separate build/update plane as options; do not infer a 1 GiB fit
  or a universal latency winner from these fixtures.

Exact top-K for arbitrary conjunction does not have an unconditional fixed
candidate-work guarantee on arbitrary data. Set resource budgets and distinguish
an explicit incomplete/exhausted outcome from a complete empty answer. Correct
term grouping and a useful ranking contract come first; measure the recall,
latency and resource frontier against them.

## FTS history that a replacement must respect

The original May 26 post, [typeahead? more like typebehind! amirite](https://waow-tech.pckt.blog/typeahead-more-like-typebehind-amirite-tzgmqge),
was retrieved in full from
`at://did:plc:siv7zbedip4vcet4v67piibr/site.standard.document/3mmqccpk45cln`.
It reports distinct failures: `alex*` with BM25 took 1.8 seconds versus 67 ms
with an unranked limit; occupied reader slots led to 503s; a supposedly
incremental sync dropped/reinserted 5.9 million rows and left FTS empty for up
to 20 minutes; rebuild writes blocked readers; and malformed `fig (` MATCH
syntax was swallowed as an empty result. The local read replica already avoided
remote reads. These are historical author-reported measurements, not newly
reproduced current-production timings.

The [May 24 design introduction](../../typeahead-index-design.md) additionally
names `éric`, `natemoo.re` and `jay.bsky.team` as slow-query examples. A future
FTS trial needs those normalization/domain/parser controls, broad and rare
prefixes, authority ranking, pool saturation, consistent incremental writes,
and storage-cold measurement. Fixing sync or quoting MATCH does not alone solve
broad-match ranking cost; historical faults also do not prove every possible
FTS retrieval design is unsuitable.

## Qualification and clean cutover design

The current evidence is sufficient to identify the bug and choose the next
prototype, not to choose a production engine or promise an outage-free release.
The next implementation gate is a native compact-posting prototype with the
existing response contract and actual update lifecycle, compared with Tantivy
under the same semantic and update workload. Choosing between those designs
requires separate native/cloud evidence. The available handle-query workaround
allows this structural work to proceed without an urgent workaround deployment.

Use independently owned cloud scratch and storage beside the cloud build source;
no corpus transit through heavypad and no serving/rollback volume experiments.
Identify and checksum the frozen corpus, generation, code and workload. Measure
correctness against independent oracles as well as old-engine differentials.
Include score changes, renames, new actors, hide/show, deletion, PDS moves,
operator precedence, duplicates/replay and rebuild equivalence. Test scoped
readiness and explicit excluded-host errors together.

Qualification must include actual 2 CPU / 1 GiB capacity if that remains the
target, process and storage-cold states separately, sustained mixed update/search
progress, queue/pool occupancy, failure-inclusive p95/p99, memory including file
cache, bytes read, WAL/checkpoint/write amplification and rebuild/replay time.
Do not substitute a median, a single common-name success or an overlay-only
baseline equality test for these gates. No host cache flush was used here.

Only a qualified generation could proceed through the existing inactive managed
slot: install independently, admit structure and semantic sentinels, replay from
its watermark, establish scope readiness, restart at final size and qualify.
Keep the serving pointer fixed throughout preparation. A conditional routing
change would then promote the ready backend while retaining the previous
qualified backend for rollback. Test rollback and freshness/correctness failure
criteria before promotion. This investigation performs none of those actions.

# Isolated native retrieval comparison

2026-10-28. Investigation stages; no production implementation or rollout is
authorized by this investigation. Refill and historical caps are comparators,
not architectural requirements. The intended matching predicate, measured costs
and safe operational behavior decide which design merits implementation.

## Stages and variants

1. **Completed:** unchanged native builder/HTTP handler, adversarial 990-actor
   fixture, 27 oracle cases plus native replay/rebuild and update-write receipts.
2. **Completed:** same native reader with complete membership for this fixture
   (`INDEX_TOP_N_*=65535`, below the format's u16 ceiling). This isolates base
   truncation; it fixes four cases, leaves ten mismatches and is not a candidate
   fix. See native-control-comparison.json.
3. **Completed pilot:** generated 1k/10k native corpora and 1k/10k/30k static
   FTS5/rank-ID plan controls; see [results](comparison-results.md). Native
   alternative engines and full lifecycle/production-capacity qualification
   remain future work. The broader comparison calls for identical
   authority scores, profiles, scopes and deterministic workload. Compare:

| Variant | Purpose |
| --- | --- |
| Current native baseline | Preserve current index and query behavior as the control |
| Native bounded batch refill | Same stored lists; vary total attempted candidates, retaining an explicit work ceiling; determine which starvation cases it repairs and which stored-tail losses it cannot |
| Native complete-membership conjunction | Full membership before top-K, full lexical predicate before the result cap, eligibility during enumeration; initially a simple native representation, then compact rank-ID arrays or bitmaps if measurements justify them |
| Native SQLite FTS5 plan | Revisit full-text retrieval with AND of prefix terms, authority ordering, indexed eligibility/PDS predicates and late profile fetch; retain EXPLAIN plans and real update costs |

A simple complete-intersection implementation is a baseline for index redesign;
block maxima/WAND or a score-aware trie should earn their complexity through
measurements. An FST is a possible dictionary representation, not a substitute
for complete membership and conjunction. Ordered phrase/shingle indexes are
optional accelerators, with reversed/nonadjacent-token controls mandatory.

## Corpus and oracle

Generate overlapping/disjoint common-token populations independently of stored
prefix lists. Include a unique joint match ranked below every selected seed
budget, a match beyond one and beyond all base caps, unrelated handles, reversed
and nonadjacent display words, long shared 16-byte prefixes, ties, exact handles,
rare matches, genuine misses, broad matches and repeated tokens. Vary population
size and token skew separately, so a larger corpus does not silently change the
query family. Keep adversarial correctness and representative workloads separate.

The independent oracle evaluates all supported lexical constraints and current
eligibility on effective actors, then exact promotion, stored authority ranking
and stable DID ties, then the requested limit. It does not use capped postings
or copy baseline responses. Normalization and supported-script policy are stated
explicitly. Start with ASCII to compare retrieval fairly; CJK/mixed-script AST,
query limits and normalization need their own semantic gate before any release
claim. Old results alone cannot be the oracle for a correctness change.

Overlay phases must cover: new actor, metadata-only update, same-content/new-
timestamp replay, score raise/lower, rename, deletion, hide/show, PDS migration,
operator precedence, repeated application and rebuild from the same effective
state. Complete membership must not regress after replay is compacted into a
new generation. A fixture-only readiness marker must not be confused with the
production scope-policy replay handshake.

## Measurements

- Correct top-K DIDs, ordering, recall, false positives and false-empty queries;
  separately count complete answers, explicit budget exhaustion and errors.
- End-to-end and engine p50/p95/p99/max, with every attempted request retained.
  Use identical request/serialization transport where possible; report remaining
  transport differences rather than attributing them to retrieval.
- Warm repeated and varied queries, process-cold restart, and mixed search/update
  phases. **Process-cold is not disk-cold.** Do not flush host caches. Actual
  storage-cold and 2 CPU/1 GiB capacity remain separate future qualifications.
- Posting entries/bytes visited, SQLite rows stepped, candidate IDs checked,
  eligibility reads, profile blobs decoded and bytes fetched. For FTS, distinguish
  matching rows from final profiles; LIMIT does not prove cheap candidate work.
- Process RSS/high-water memory, bounded reader buffers and index cache; on a
  future isolated VM include file-cache pressure, not just mapped RSS.
- Snapshot/index/journal bytes, build time and peak scratch; update wall time,
  commit/checkpoint time, SQLite changes, WAL/data bytes, update throughput and
  replay lag. Do not compare actor-only writes with full old projection and
  omit the new design's index maintenance.

Sweep budgets and report recall/latency frontiers rather than selecting one cap
in advance. A proposed initial sweep can use 50/200/1000 candidate checks and
separate posting-byte/profile limits, adjusted to the actual representation.
Budget exhaustion must be explicit in internal evidence; it must not be counted
as an exhaustive empty answer. Do not infer a corpus-independent latency bound
from finite measurements. General conjunction plus ranking can require work
that grows with the memberships examined; fixed work and unconditional exact
recall cannot simply be asserted together.

## Matching and resource design to test

Use an explicit query expression: handle/exact/domain alternatives OR the AND of
all display constraints, including multiple fragments emitted from one CJK or
mixed-script token. PDS and eligibility apply before final selection. Keep exact
promotion and ties explicit. Evaluate full query strings before truncating to
profiles; do not discard the tail information needed to verify a long prefix.

A compact complete-membership option assigns generation-local integer actor IDs
and stores complete, ordered postings with small per-block metadata. If IDs are
ordered by immutable authority score and DID tie-break, conjunction can seek in
rank order and stop after enough eligible matches. Score changes need a coherent
delta merge; stale base scores cannot serve as valid bounds for new higher
scores. Sparse arrays, delta compression and bitmap containers have different
costs and should be measured rather than assumed universally superior.

Store canonical profile data separately. A small current-state/eligibility view
can reject tombstones, hidden actors and obsolete PDS identities before decoding
full profiles. All candidate and eligibility reads for a request need one coherent
base generation plus overlay view. A delta may store each logical actor change
once and maintain complete membership for changed names, rather than durably
rewriting every prefix timestamp, but its WAL/index/compaction costs and memory
must be included. Do not drop source-version barriers just because bytes match.

## Safety and rollout design

Current stage is local synthetic data only; it uses no production credentials,
no production workload, no cache flush and no corpus copy. Before increasing
scale, declare owner, private scratch, disk/wall budgets, retained evidence limit
and automatic teardown. Shared host compiler cache is not a disk-cold device.

A production-sized experiment, if later warranted, must use independent cloud
storage and a frozen, identified input; never serving or rollback storage in
place. The corpus must not transit heavypad. A qualified release would require:

1. Versioned checksummed generation tying dictionary, postings, rank/identity
   mappings, profiles and scope policy together; no mixed-generation IDs.
2. Offline build and structural admission; replay from the snapshot watermark,
   including changed-scope readiness and operator precedence.
3. Independent semantic oracle comparisons and bounded saturation/failure tests,
   then native and HTTP qualification at the actual 2 CPU/1 GiB target, including
   sustained update progress. Preserve aliases and cache/scope acknowledgments.
4. Existing managed inactive-slot installation, restart at final size and
   qualification while the current serving backend stays fixed.
5. Conditional route promotion with the previous qualified backend retained;
   rollback on correctness, availability or freshness failure. Compare fixed,
   failure-inclusive public windows and network locations without load-testing
   production. Local fixture timings alone cannot authorize this cutover.

These are future gates, not actions performed or authorized by this report.

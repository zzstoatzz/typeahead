# Opt-in global rank merge

October 29, 2026. Local synthetic qualification only. The root controller owns
cloud deployment, paired full-corpus measurement and any later default change.
The default remains `--retrieval per-segment`; `serve --retrieval global-merge`
selects the experiment. Both strategies use the same canonical eligibility,
identity exclusions, exact/PDS partitioning and exact boosted-score comparator.

## Collection and stopping proof

For each exact/nonexact and optional local/remote bucket, the global path opens
one scorer and rank column per captured segment. A min-heap contains each
nonempty stream's current `(rank, segment, document)` head. Rank is the committed
ascending encoding of descending finite f32 score and ascending DID. Pop the
best head, charge the candidate budget including deleted documents, hydrate and
apply canonical policy plus CJK verification, then accept or reject. Only the
popped stream advances. Collection stops after K eligible profiles in the bucket;
it does not read the next head after the Kth accepted profile.

Within one bucket, exact/local priority is constant and any score boost is a
constant. An eligible profile after that bucket's Kth valid profile has at least
K valid profiles ahead of it in the final ordering, so it cannot enter the
combined top K. The existing final merge applies exact priority, optional
PDS-first priority, exact mathematical f32 base-plus-boost ordering and DID ties.
The TwoSum comparator is shared by both strategies; a plain f64 addition could
otherwise collapse distinct ranks under a huge boost and invalidate this proof.

One immutable Searcher/policy generation supplies rank, eligibility and profile
payloads. Accepted hydrated profiles are reused for output. Deleted documents do
not consume accepted slots. Rejected CJK/moderation candidates refill until K
qualify or the explicit candidate budget fails. A malformed expiry encountered
while establishing eligibility fails with 503. The global path may avoid
examining an irrelevant lower-ranked malformed profile; full-source expiry
coverage remains an independent gate.

## Local evidence

Actual Tantivy indexes with merge disabled only inside the Rust test fixture
retain exactly 8 and 32 segments. Each fixture has 64 actors per segment. Raw
profile oracles cover intersections, empty matches, ties, negative scores,
rounded f32 ties, exact priority, hard scope, PDS preference and CJK contiguity.
The two fixtures make 400 independent oracle comparisons across both strategies,
plus mutation/deletion, budget and concurrent-generation checks.

| Synthetic fixture | Per-segment hydrated/visited | Global hydrated/visited | Global rank-head reads | Scorers opened by either strategy |
| --- | ---: | ---: | ---: | ---: |
| 8 segments, 512 actors | 86 | 12 | 19 | 16 |
| 32 segments, 2,048 actors | 346 | 12 | 43 | 64 |

The twelve global visits include moderation rejections before ten accepted
profiles. Both paths still perform scorer setup across all segments. These are
work counts, not timing ratios or corpus-scale capacity claims. Rank-head reads
include heap initialization heads that might never be popped; visited candidates
count popped/scanned documents, including deleted documents. Neither count
measures Tantivy's internal posting decode/seek work or provider disk reads.
Debug output additionally records unique segments visited/hydrated, actual
hydration count/time and moderation time.

The actual HTTP suites pass for both flags, including more than seventy false
CJK candidates, source restart/crash behavior, policy precedence/refill, strict
scope admission on direct/canonical/alias endpoints, and runtime fingerprints.
An independent Python Fraction oracle covers 192 boosted-score queries per
strategy, including huge boosts, subnormals, negative bases, cancellation and
true sum ties. Receipts name exact tested executable hashes. Fixture payloads
and servers are retired after each run.

No private-file eviction, OS-cache-cold timing or actual-corpus latency conclusion
is claimed here. Those comparisons must serialize measurements, identify cache
state and use the same resources and admitted snapshot. The experiment remains
opt-in until correctness and paired real workload timing gates pass.

## Runtime identity and admission

At startup, the server hashes the executable once using a bounded 1 MiB buffer
and caches the digest. Linux opens `/proc/self/exe`, retaining actual running-inode
identity if an installed path changes. Successful public responses advertise
engine, captured generation build/format, actual executable SHA-256 and retrieval
strategy. `/index/status` exposes the same binary/strategy and immutable query
configuration. A fixture atomically replaces the executable pathname while the
old process serves and verifies the live fingerprint remains the original hash.
An installed-file hash alone therefore cannot qualify a stale running process.

Fly machine/instance identity is also captured once from `FLY_MACHINE_ID` and
`FLY_MACHINE_VERSION`. Present values must be 1..128 ASCII alphanumeric, hyphen
or underscore characters; malformed values fail startup. Local missing values
remain null with no corresponding header. Real Fly admission requires the
runtime values and matches them against provider attestation, while rejecting
configuration overrides of reserved platform variables. Twenty actual startup/
HTTP checks cover missing/local identity, controlled environment values and
invalid metadata. These tests do not claim an external provider attestation.

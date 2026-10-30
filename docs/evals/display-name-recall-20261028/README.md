# Candidate recall investigation

2026-10-28. Investigation only. No runtime source, production data, configuration,
image, resource size or routing change is part of this goal. All new fixtures
are synthetic and local. The earlier live report and bounded traces are in
[the incident record](../../ops/display-name-recall-20261028/README.md).

## Established findings

The final native fixture has 990 actors and 27 explicit oracle cases. Thirteen
match the oracle; fourteen expose a defect or the separately identified eight-
token coverage boundary. This is an adversarial test set, **not a prevalence
estimate**. The generator, unmodified native builder, real SQLite files, native
loopback HTTP handler and serializer are exercised. The names, DIDs and oracle
answers are synthetic, including a matching display name whose handle is
unrelated to the query. See [results](native-fixture-6/results.json) and
[generator](../../../experiments/recall-scope/run.py).

| Boundary | Reproduction | Consequence |
| --- | --- | --- |
| Independent base-list top-N | `Justin Fa` and reversed `Fa Justin`; matching actors are in `n:justin` but omitted from capped `n:fa` | Empty intersection although complete membership has matches |
| Scope-local top-N | Same query with `pds=scope.test` | Filtering by PDS before caps is necessary but does not cure truncation inside a scope |
| 16-byte cut plus base top-N | Selective long handle prefix shares its first 16 bytes with higher-scoring decoys | True match absent from the stored list |
| 50 survivors before long-query checks | A true match is present in the 61-entry base list but below 50; the first 50 fail the full query | Empty result despite sufficient membership in the stored list |
| 50 survivors before moderation | First 50 of 51 matching actors become hidden | Correctly suppresses hidden actors, but never fills from the remaining visible actor |
| Deleted capped base entries | The stored top 200 are deleted; a lower actor remains in the source | Tombstones are respected; missing tail cannot refill |
| CJK/mixed-script grouping | Unspaced `日本太郎` / `alice日本` union their emitted fragments | False positives without any cap being reached; spaced conjunction controls pass |
| Eight-token indexing/query limit | A ninth display token is omitted; a ninth query token is ignored | Documented indexing bound and silent query weakening are distinct from top-N loss |
| Exact promotion exception | 101 DIDs share one exact handle | 100 candidates are hydrated despite nominal `hydrate_cap=50`; exact promotion is not a hard 50-profile bound |

Native reprojection of the two matching actors restores both broad and scoped
`Justin Fa`. A real fresh builder run with the same actor state and a new empty
overlay loses them again. The rebuild does fix the fixture's hidden/deleted
starvation because those actors are removed before the new top-N is chosen.
Thus rebuilds and replay can change apparent recall without a new ranking rule;
blindly reprojecting names is not a durable fix for intersection loss.

The existing bounded/unbounded differential compares **overlay read strategies**
on the same capped base and hydration logic. Matching that baseline does not
prove lexical recall, intended query semantics or refill completeness. The
canonical `fig aka` structural check exercises a named multiword match, but not
the common-token truncation counterexample. Future gates need independent
oracles and adversarial overlap, not only byte equality with the old engine.

## History and attribution

- `786eb0e` (May 24): original builder already keeps tiered top-N (300/200/100).
- `fa2e37f` (May 25): index serving intersects those lists, merges down to 50,
  then hydrates and suppresses hidden/deleted rows.
- `d63beb4` (May 26): CJK tokenizer added. The merge mode remains selected by
  ASCII whitespace, although the CJK design document says bigrams intersect.
- `ea80ca4` (September 18): long-query key cuts and checks **after** hydration.
- `209c6c5` (September 9): union overlay optimization explicitly exempts
  intersections and exact keys from its bound.
- Current investigation source `6554864` has no changes to `services/src/index`
  or `services/src/search.zig` relative to retained native `62266a3`.

These dates establish code history, not the first affected production request.
The incident's matching empty answers from both managed backends establish that
today's response containment Worker is not responsible for this query failure.
The source predates the October 27 performance work. Earlier reporting that
located the cap defaults at the May 26 directory move was incomplete; following
the moved files finds the May 24/25 implementation.

## What the small design controls establish

[Algorithm controls](design-controls-final.json) are independent ASCII fixture
models, not native fix implementations or latency measurements.

| Candidate | Finding |
| --- | --- |
| Increase per-key caps | Doubling fixes the 990-actor fixture; counterexamples at 300, 600 and 1,200 each still fail just beyond the chosen cap |
| Union, take 50, then verify all words | Misses the names and can regress currently working selective conjunctions when unrelated popular single-word hits occupy the budget |
| Rarest token, take 50, then verify | Misses `Justin Fa`: the two matches are below the seed's first 50; can also miss actors absent from every stored list |
| Ordered phrase-prefix fast path plus old retrieval | Helps forward `Justin Fa`, but reversed `Fa Justin` still fails; single-string completion is not arbitrary-order conjunction |
| Remove spaces and search handles | Cannot recover the matching display-only actor with an unrelated handle |
| Complete membership, conjunction, then top-K | Matches all five modeled ASCII controls; its resource behavior and production-scale implementation are unqualified |

Bounded refill remains a useful **comparator**, particularly for post-filter
starvation. It is not a required architecture or a predetermined solution.
Historical cap and O(1)-in-corpus aspirations are context; alternatives must be
judged by intended semantics and measured search, storage, build and update costs.
No experiment relaxes production's current operational contract.

## Current storage and update observations

The final synthetic snapshot is 3,006,464 SQLite bytes. Its 16,426 keys contain
62,226 retained memberships in 1,494,158 packed bytes. These are fixture-only
figures and cannot predict a full-corpus replacement's size. This format repeats
a score and variable-length DID in every retained membership.

Native projection of two unchanged-content actors writes two actor rows and
160 prefix rows (global and scoped). A second projection with only timestamps
advanced reports 162 SQLite row changes again. This is an observed version-write
amplification example, not a measurement of all production update distributions.
Removing timestamps naively is unsafe: a replacement snapshot may capture an
intermediate source state the overlay never observed. Any compact delta design
must preserve the newest actor version through watermark compaction, renames,
score changes, deletions, operator overrides and PDS moves.

The separate writer-read-mode performance trial failed on the larger overlay;
see [its result](../sync-performance-20261028/README.md). More cache or a new
candidate dictionary alone cannot be assumed to solve that storage interference.

## Investigation limits

No corpus-wide affected-account fraction has been measured. The only real-world
example established here is the previously captured Justin Fagnani query family.
CJK, token limits and lifecycle cases are proven against the same native search
source on synthetic data; this is not a survey of production-language traffic.
The membership loss mechanism is established independently; the public debug
trace does not itself expose every stored DID or the discarded tail.

Full-handle FST results from the earlier experiment do not establish display-name
conjunction, dynamic eligibility, scope-before-caps or a complete retrieval fix.
An FST can locate terms or ranges; retaining and correctly combining memberships
is a separate requirement.

## Reproduction and lifecycle

```sh
python3 experiments/recall-scope/run.py /absolute/new/evidence-directory
python3 experiments/recall-scope/design_controls.py \
  /absolute/new/evidence-directory/fixture-actors.json /absolute/new/controls.json
```

Owner: `display-name-recall-20261028`. Scoped temporary scratch budget 512 MiB,
15-minute wall bound and a 1 GiB free-space reserve. Build uses the repository's
existing Zig dependency/cache setup; runtime environment contains no service
credentials and `INDEX_CHANNEL=local` disables publication. Servers bind only
loopback. Success, failure and interruption stop owned process groups and remove
scratch. The final run's peak retained scratch at cleanup was 16,358,977 bytes;
its receipt confirms all children terminal and scratch absent. Compiler cache is
outside this payload figure. No corpus, production mutation or host cache flush
was used. Earlier failed harness runs and superseded fixture outputs are retained
with their cleanup receipts; `native-fixture-6` is authoritative.

The [native comparison plan](comparison-plan.md) covers the broader design
space, empirical metrics, budget-exhaustion semantics and future guarded rollout.
The [native complete-membership control](native-control-comparison.json) is
complete: it fixes four oracle cases and increases fixture snapshot size from
3,006,464 to 3,592,192 bytes. Ten oracle cases still mismatch; timestamp-only
write amplification is unchanged. The [expanded native comparison](comparison-results.md) tests refill versus
complete retrieval at 1k/10k actors with real mixed projection, plus static
FTS5/rank-ID controls through 30k. Complete retrieval repairs the ASCII cases
but exposes the cost of the existing representation. The
[fix recommendation and cutover design](recommendation.md) calls for complete
compressed memberships, ranked enumeration and eligibility before selection.
The investigation is complete; no production fix is qualified or deployed.

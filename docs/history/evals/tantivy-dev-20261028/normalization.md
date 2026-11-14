# Native normalization for the parallel Tantivy backend

October28,2026. Implemented locally in `services/tantivy-normalizer`;
this adapter has not been deployed by this task. The parent owns devbackend
import, cloud lifecycle and hard smoke qualification before any cutover.

## Implementation

The C ABI directly imports canonical Zig `normalize.zig`, `search.sanitize` and
`pds.zig`; no independent Rust normalization tables or NFKC prototype fallback
are present. `normalize_version=v2-cjk-bigram` is pinned independently from
`plan_version=complete-prefix-groups-v1`. Built index metadata must carry both
and the serving process must reject mismatches.

The adapter exposes metadata, actor key generation, grouped query planning,
full candidate lexical verification, canonical PDS normalization and native f32
score conversion. Requests/replies are owned JSON buffers through `ta_plan`;
`ta_result_free` releases each result. Host static/dynamic builds and Linux
x86_64-gnu static cross-compilation succeed with Zig0.17.0. The Rust backend can
compile the static adapter in its own target workspace/container. Full ABI and
operation details are in [the adapter README](../../../services/tantivy-normalizer/README.md).

## Correctness improvements and intentional policies

The canonical text folds, kana/width behavior, underscore distinctions and
query sanitation are reused unchanged. Complete Latin prefixes and every display
word remove the legacy16-byte prefix and eight-token retrieval cuts. The adapter
also removes the24-codepoint CJK key-generation cut. UTF-8 prefixes always end
at complete codepoint boundaries.

CJK key emission remains linear: one leading unigram and adjacent bigrams. A
64-codepoint single CJK name emits64 keys, rather than2,016 substring keys.
Queries require all groups; a single-token handle/domain alternative surrounds
that conjunctive display branch. Flattening CJK bigrams or mixed-script groups
into a single OR is incorrect.

Bigram membership alone can falsely match a query such as `日本太` against
separate `日本` and `本太` name runs. The query plan marks that need for
verification. The candidate predicate requires the complete CJK substring to
occur contiguously within one normalized CJK run. One-character CJK queries
retain leading-unigram semantics; longer CJK queries can match nonleading
contiguous portions of a run. Mixed script groups are not cross-script phrase
constraints; whitespace words remain order independent.

The backend agent confirmed that verification occurs before candidate acceptance
into top-K, inside the same captured searcher view, with continued candidate
iteration rather than an arbitrary50-candidate cap. Exhausting its candidate
verification budget must return an explicit503 search-budget error, not a
complete empty response. Backend integration still needs to demonstrate this
in hard smoke tests; adapter tests alone cannot establish correct collection.

The canonical normalizer can truncate at its output cap. This adapter instead
returns `budget_exceeded` if meaningful normalized content would be discarded.
Exactly256ASCII bytes are accepted,257 meaningful bytes fail, and longer raw
strings that fold below256 bytes remain accepted. The CJK normalization cap's
byte-boundary slack is rejected if it exceeds256 output bytes. Known unsupported
query scripts return `unsupported_query` rather than silently disappearing and
broadening a mixed query. Those are explicit plan-policy differences, not a
claim to expand canonical normalization's script coverage.

PDS host parsing is exact canonical behavior. Identity ownership and validity of
imported actor handles remain the importer's responsibility. Float scores are
rounded to native f32, with nonfinite/overflow values rejected; numeric signed
zero and f32-rounding ties require DID tie breaks in the Rust rank encoding.

## Verification

- Six native adapter tests passed.
- Nine actual C ABI tests passed, including repeated allocation/free calls.
- Canonical native normalizer's23 tests passed.
- Canonical PDS and search modules' one test each passed.
- Linux x86_64-gnu static cross-build succeeded.

The tests use explicit normalized text expectations, long-prefix/ninth-token
positive and negative controls, a CJK cross-run false candidate that must be
rejected, mixed-script partial matches,64-codepoint CJK emission, strict PDS
inputs, f32 ties and nonfinite values, and output/input bounds. They do not merely
compare two index implementations sharing the same wrong prefix plan.

[Source hashes and receipts](../../../services/tantivy-normalizer/verification.json)
record the adapter and canonical inputs. No credentials, cloud resources or
production data were touched by this task. Existing root README edits were
preserved; no production behavior changed, so this task does not update them.

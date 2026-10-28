# Full-handle candidate sources

Experimental standalone Rust program comparing a sorted full-handle dictionary
with `tantivy-fst` plus `levenshtein_automata`. No public listener or production
configuration is included.

```sh
cargo build --release --locked
cargo test --locked
python3 test_sidecar.py
python3 test_export.py
```

`export.py <flat-snapshot> <overlay-db> <new-output-directory>` streams actor
records from the immutable FLATPX05 snapshot and applies the complete frozen
overlay by DID. Hidden, invalid and unsupported malformed handles are excluded.
It writes full handles, stable DIDs, f32 quality scores, hosting PDS and profile
locations. Sort the TSV with C collation by handle then DID before `build`.

```sh
sort -S 128M -t "$(printf '\t')" -k1,1 -k2,2 unsorted.tsv > sorted.tsv
./target/release/handle-sidecar build DATA_DIRECTORY
./target/release/handle-sidecar check DATA_DIRECTORY fst SNAPSHOT CASES_JSON 20000
./target/release/handle-sidecar bench DATA_DIRECTORY sorted SNAPSHOT CASES_JSON 20000 640 20 hydrate
```

Cases contain `id`, ASCII handle-prefix `q`, optional `fuzzy` and optional `pds`.
Both implementations use shared mmap-backed variable-length records. The sorted
control uses an ordinal-offset array; the FST maps each unique handle directly
to its first record byte offset and does not load the ordinal-offset array. Duplicate handles preserve every DID. This shared representation
isolates traversal cost but does not measure an optimized FST-only encoding.

The sorted fuzzy control generates insertion, deletion, substitution and adjacent
transposition variants and searches their prefix ranges; it does not scan all
handles. The FST intersects the dictionary with a distance-one prefix automaton.
Both promote complete exact handles, then rank by edit-distance tier, descending
quality and DID, deduplicate DIDs,
and hydrate at most 50 profiles before returning ten results. Fuzzy mode is an
explicit measurement mode rather than the proposed public fallback policy.

Candidate enumeration stops at an explicit traversal budget and reports
`truncated`. This can yield different partial top tens between implementations.
PDS filtering precedes ranking, but the traversal budget can be exhausted before
finding scoped matches: this is not an implementation of the public scope
contract. Complete recall is claimed only for untruncated cases. There is no
range top-k structure, display-name or domain-suffix index in this prototype.

`run_remote.py` operates only inside the separately owned private Fly volume.
It runs ABBA order, includes scheduling delay in latency, uses the real retained
native projection/checkpoint writer, preserves raw measurements and removes
its research payload. The sidecars are frozen effective views; the native replay
changes timestamps on the same captured actor content. This measures interference,
not atomic sidecar update publication, new-actor discovery or freshness. Native
sync-prefix writes are retained. The comparison does not prove release readiness.

`manage.py` is the experiment's host-side lifecycle supervisor, pinned to the
recorded owned Machine name and independent volume. It collects only bounded
compressed evidence and retires the owned Machine and volume. It must not be
reused with different resources without updating the ownership guards.

Profile reads use exported direct offsets and do not execute the serving map's
block-admission verification or HTTP response encoding. Absolute timings cannot
be presented as native engine or public request latency. Cache-reset phases use
advisory eviction; they do not prove every payload page starts nonresident.

The sorted control binary-searches the common actor records, rather than a
separate key-only string pool. A key-only sorted dictionary, narrower offsets
where file size permits, and an FST encoding without duplicate handle strings
are further layout controls. This experiment does not establish an optimal
encoding or attribute every observed gain to automata alone.

The workload uses systematic samples of stored handles and generated edits;
it is not a production-query trace. Scheduling delay remains in reported latency.
Late computations finish and remain in the results; the harness does not cancel
each query at an HTTP deadline. The report counts responses beyond one and ten
seconds separately. Mixed phases request 640 lookups at 20/s with four workers;
sync batch counts and throughput must accompany their percentiles. These short
windows do not qualify the issue's sustained 30-batch release gate.

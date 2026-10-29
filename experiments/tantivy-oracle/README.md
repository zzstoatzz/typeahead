# Frozen raw-profile reference

This experiment computes ordered top K directly from raw profiles. It does not link Tantivy, read its index, or use search responses as expected results. Run full-corpus work only in the owned cloud qualification scratch directory. No production writes or traffic are needed.

The canonical Zig normalizer and query planner supply text projections and validated query groups. Independent Rust code matches raw normalized handle/domain prefixes and display script runs, then ranks by exact-handle priority, requested PDS policy, exact f32 sums, and DID. Boost arithmetic uses signed 320-bit integers on the 2^-149 grid rather than the serving engine's TwoSum comparator. A compact experiment-owned projection avoids generating thousands of actor prefix strings, with canonical actor-budget parity tested separately.

## Build and verify

Dependencies are Zig 0.17.0, Rust/Cargo, Python 3, and cached locked Cargo dependencies. Cross-building also needs cargo-zigbuild and the Rust target.

```sh
python3 experiments/tantivy-oracle/build.py
python3 experiments/tantivy-oracle/test_oracle.py
cargo test --release --locked --offline --jobs 2 --manifest-path experiments/tantivy-oracle/Cargo.toml
python3 experiments/tantivy-oracle/build.py --zig-target x86_64-linux-gnu --rust-target x86_64-unknown-linux-gnu.2.28
```

Host binary: `target/release/tantivy-raw-oracle`. Linux binary: `target/x86_64-unknown-linux-gnu/release/tantivy-raw-oracle`. Build products remain inside this directory; canonical adapter sources and service build products are unchanged.

## Cloud invocation

Freeze cases from the first 10,000 raw actors, then run the reference once over the complete raw stream:

```sh
/app/tantivy-raw-oracle freeze --input /data/qualification/run/sample-prefix.jsonl --output /data/qualification/run/cases.json --identity-policy /app/identity-quarantine.json --sample-actors 10000 --cases 512 --updates /data/qualification/run/updates.json --native-scopes /data/qualification/run/native-scopes.json
/app/tantivy-raw-oracle run --input - --queries /data/qualification/run/cases.json --updates /data/qualification/run/updates.json --output /data/qualification/run/oracle.json --identity-policy /app/identity-quarantine.json --source-kind frozen-materialized-hidden --max-seconds 1800 --max-rss-mb 256
```

Pipe the existing `services/tantivy-input/export.py --output -` into the second command with pipeline failure propagation. The source schema is unchanged: full manifest, strictly DID-sorted actor rows with snapshot-watermark versions, and verified count/SHA256 footer. No second full JSONL file is required. `sample-prefix.jsonl` contains that exact original full manifest and its first 10,000 rows; omit the footer for a bounded prefix. It is a sample, never an admitted full export. The full pass verifies its prefix bytes/hash, build, watermark and declared count against the frozen case metadata.

The supervisor owns the scratch root and exporter/reference process group, enforces the overall deadline and disk budget, and removes sample/feed/oracle payloads after the qualification receipts are retained. The runner additionally enforces 4 MiB records, 64 GiB input, 512 cases, K≤100, 256 affected identities, 64 update batches, 64 MiB output, and a configurable resident-memory ceiling. It publishes answers only after complete source verification and successful checkpoints. Existing output files are never overwritten.

## JSON contracts

`cases.json` is `{schema_version:1,source_kind:"frozen-materialized-hidden",cases:[...],sample:{...}}`. Each case contains `id,q,limit,category,native_limitations,native_shared_features` and optional `pds,prefer_pds,preference,boost`. Prefixes are deterministically hash-ranked within length strata from raw handles. Curated, raw-derived and bounded update-feed-derived cases cover exact handles, both multiword orders, long prefixes, CJK, hard PDS scope, preference, finite extreme boosts and limit 100. The first-N DID sample is not a uniform corpus sample; its method and limits are recorded.

`updates.json` uses the agreed full-bootstrap schema: version 1, source kind, build ID, source watermark, `baseline_actors`, and ordered `batches:[{batch_id,cursor,expected_cursor,actors}]`. The runner hashes the exact bytes, checks baseline matching/ranking fields and version against the raw stream, and applies stale/equal-conflict rules. Hydration-only fields are outside this reference's equality check. Replaying a current batch does not create another oracle checkpoint.

`oracle.json` is `{schema_version:1,receipt:{...},checkpoints:[{cursor,queries:{CASE_ID:{status,ids,native_limitations,native_shared_features}}}]}`. Checkpoint 0 is the snapshot; later cursors are the batch boundaries. Each ID list is the same independent expected truth for both engines. Query 400 and successful empty 200 are distinct. The receipt pins source hashes/counts, raw-sample provenance via the case hash, exact feed bytes, identity-policy hash, runtime binary SHA256, normalizer/planner versions and PDS metadata gaps.

`native_shared_features` means the request uses global lexical search semantics already attempted by the native engine, or a hard PDS host proven supported by the pinned native scope roster. The optional `--native-scopes` input is `{schema_version:1,build_id,source_watermark,hosts:[...]}` from enabled `pds_scopes` in that same immutable metadata database; its exact SHA256 and host count are recorded. It must contain unique canonical hosts and stay within 100,000 hosts / 32 MiB. Unsupported/unproven scopes and contextual preference are false. It is not a correctness certificate: native candidate caps, truncation and conjunction misses remain real mismatches. Compare speeds on shared requests and report their correctness failures; report new-feature absolute latency separately.

## Bounded checkpoint proof

The one complete raw pass excludes every feed-affected DID and retains only the best K unaffected actors per query. At each checkpoint it merges these K with all bounded affected actor states. An unaffected actor below position K cannot enter the combined top K when adding actors, so deleting, hiding or rescoring any affected actor cannot expose a missing unaffected winner. No full pass per checkpoint and no corpus-sized postings are needed.

The reference applies the snapshot's materialized `hidden`/`deleted` flags and canonical quarantine. It does not reevaluate raw labels, source-wide expiry rules, operator state or source freshness. Those remain separate live-source qualification gates. Normalization/query budgets fail explicitly; the reference does not silently skip oversized actors. A synthetic pilot receipt is not a full-corpus or cloud performance result.

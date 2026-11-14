# Source timestamp-only observations

October 29, 2026. Local paired experiment against immutable executable copies;
no cloud or production source contact. The source-authority path now recognizes
an unchanged validated Actor even when its incoming `version` differs.

## Semantics

Inside an accepted source-owned batch, clone the complete validated incoming
Actor, align only its version with the stored Actor and compare full derived
Actor equality. If equal, retain the original stored document and timestamp,
count a replay, and commit the new observation revision, cursor, request hash and
retry receipt normally. A field whitelist is deliberately not used: all declared
Actor payload fields participate, including counts, activity, labels, associated
JSON, PDS, score, visibility and deletion.

The stored timestamp can remain the coarse snapshot watermark or the last
projection-write timestamp. It is not a claim about the latest observed source
timestamp and does not order authoritative source observations. The durable
observation revision, cursor and exact request identity establish their order.
Changing only version under a reused batch ID remains a conflict; ignoring that
field for projection writes does not remove it from request identity.

Generic apply before source migration retains its strict higher/stale/equal
version rules. Generic writes remain fenced after source-authority migration.
An actual same-second profile, hide, deletion or restoration change is written.
No format/schema change or flag is needed for this source-mode no-op behavior.

## Actual local experiment

A 32-actor operational pilot passes before the larger comparison. The paired
fixture imports 5,000 synthetic profiles through the real importer and applies
three 500-record-batched timestamp passes: higher than the seed, lower than the
seed, and equal to the seed. Each build runs actual localhost Rust HTTP servers,
uses source-state document CAS and restarts between passes. Binaries are copied
into private immutable paths before testing.

| Metric | Frozen baseline 990 | Source no-op build 4fd417 |
| --- | ---: | ---: |
| Submitted actor observations | 15,000 | 15,000 |
| Atomic metadata commits | 30 | 30 |
| Logical document writes | 15,000 | 0 |
| Replayed observations | 0 | 15,000 |
| Cumulative indexed memberships, initial → final | 292,780 → 1,171,120 | 292,780 → 292,780 |
| Immutable segment bytes, initial → final | 563,818 → 689,975 | 563,818 → 563,818 |
| Final live segment count | 3 | 1 |

The optimized immutable segment file hashes and all document digests remain
unchanged through every pass. Its total index-file footprint changes from
566,483 to 566,826 bytes, including the new durable metadata. Baseline segment
footprint peaks at an observed checkpoint of 937,781 bytes. These are retained
file footprints, not cumulative block-device bytes written; background merging
changes the baseline checkpoint shape. No timing ratio is claimed.

The semantic suite also changes every non-version payload field, checks generic
barriers, verifies changed-body batch-ID rejection, recovers identical retry
receipts after restart, and aborts real processes immediately before/after a
metadata-only source commit. Documents, cursor and observation revision recover
atomically. Core dumps are disabled, and fixture indices/servers are removed.
Receipts record both executable hashes and the optimized source reconstruction
from the frozen 990 source manifest plus the equality-only branch.

## Remaining read cost and source caveats

An unchanged source observation still performs one external source-state DID
lookup, one source CAS DID lookup under the writer lock, and one old-record
lookup for full Actor comparison: three index query/stored-profile hydrations
per actor. A written actor additionally performs the old-record lookup used for
index accounting. These are structural call-path counts, not measured runtime
instrumentation or a throughput result. The HTTP/SQL source read, serialization,
normalization and durable metadata commit also remain. Reusing validated old
records under the writer lock could reduce duplicate lookups, but that is a
separate change and is not included here.

The compact snapshot exporter initializes followers/follows/posts/activity to
zero. A later source row with nonzero values is a real payload change and still
reindexes. The actual source fraction that is unchanged has not been measured;
no claim that all 13 million actors avoid writes follows from this fixture.
Measure replay/write fractions and full-size sweep throughput before changing
operational pacing or claiming a one-day reconciliation SLO.

Unknown expiry coverage on an older index remains unknown. Replaying an unchanged
projection does not establish a source-wide coverage audit. New coverage-gated
formats must remain unready until a full admitted import or explicit offline
stored-profile audit binds complete coverage.

## Combined expiry-v4 verification

The later expiry-v4 binary
`cabfe5b295bcc7fbeba5e8e09878d8af146b2db0fe57ba2154087e636b5658ff`
also passes the 5,000-profile, three-pass experiment: 15,000 replays, zero document
writes and thirty metadata commits. Its complete expiry evidence object remains
exactly unchanged, with 5,000 covered live actors and readiness true at every
checkpoint. Its immutable segment bytes/hashes and all document digests remain
unchanged. The complete actual source-authority and global-merge policy/crash
integration suites also pass with strict expiry-v4 format assertions.

The isolated historical 990/4fd417 executables were retired after preserving
comparison receipts, source-file hashes and the exact equality-only source
reconstruction. The cleanup receipt records retirement; fixture servers and
indices are absent. The expiry-v4 tests used their own immutable private copies.

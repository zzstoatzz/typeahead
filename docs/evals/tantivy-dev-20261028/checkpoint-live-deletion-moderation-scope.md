# Deletion and moderation witness scope

Observed October 30, 2026 on managed candidate `typeahead-tantivy-a`, machine
`e820354f119698`, build `b1793346536-cp-7c8322df79`. No source/backend mutations,
restart, second reader, corpus copy or native/production request was performed.

The [initial live audit](checkpoint-live-source-audit-first.json) passed eight
source-backed exact/hydration/PDS pairs while background replay advanced revision357
to425. Its recent tombstones had no old handles; its actor sample had no independently
established moderation-absence witness. These were unavailable witnesses, not passes.

A [small read-only follow-up](checkpoint-live-moderation-tombstone-witness.json)
confirmed eight currently deleted source records and stable indexed fingerprints.
The complete source policy contained no DID hide overrides to select for an exact
moderation-absence probe. Other label/slur/domain moderation is not thereby disproved.
A non-null `/admin/source-state` hash alone cannot distinguish a live actor from a
retained tombstone: the engine intentionally stores deleted documents durably.

The [deleted-document hash probe](checkpoint-live-deleted-document-hash.json) then
matched all eight stored hashes against the entire canonical `Actor` representation:
`deleted:true`, empty profile/PDS fields, empty labels/associated, zero score/counts,
`hidden:false`, and version0. SHA256 uses the exact compact UTF-8 serialization and
field declaration order in [Actor](../../../services/tantivy/src/actor.rs) and
[the source-state digest](../../../services/tantivy/src/engine/source.rs).
The candidate version set was bounded to0, snapshot floor, current source actor
version and tombstone timestamp. A mismatch would have remained inconclusive.

All eight matched version0; the snapshot watermark was1793346236. Initial import
requires every actor's version to equal that watermark, and an unchanged canonical
replay preserves its prior version. These records therefore show post-import source
observations storing canonical deleted state, rather than merely inheriting initial
tombstones. Selected document state stayed stable and revision1036 stayed unchanged
through the probe. Three indexed/PK source SELECTs transferred3447bytes. The preceding
policy probe used five SELECTs/5777bytes. No raw actors, private policy or credentials
were returned to the operator.

This establishes deployed deletion consumption/state parity for eight sampled DIDs.
It does not observe an old public handle disappearing, or a live source moderation
transition. The same frozen D1 controlled qualification covers those causal behaviors;
injecting synthetic observations into the canonical candidate would corrupt provenance
and still would not establish ordinary source propagation. No such injection is needed.

The remaining deployment proofs should use the existing operations, not another
engine benchmark: controller-owned cached restart/recovery and fresh instance admission;
the new candidate/preview HTTP smoke with exact identities; isolated route/cache/CAS
promotion and compensation; and final serving-shape resource/source-freshness receipts.
Actual daily full-round renewal should be distinguished from the admitted complete
checkpoint plus fresh remote handoff; its expiry supervisor must remain active.
Production routing and Worker/transport deployment still require the user's approval.

# Independent dev-backend audit

October28,2026. Focused review of `services/tantivy` and `services/tantivy-input`
for the parallel dev deployment. This is not a general security audit or an
approval to cut over production. Only local synthetic data and loopback HTTP
were used; no cloud credentials, resources or production source were accessed.

## Hard gates exercised

The independent actual-binary suite is
`services/tantivy-normalizer/test_backend_adversarial.py`. Its five tests passed
against stable binary SHA256
`8e99f1b592f0d468174b9505e5b70ccdf944ce8a40c7cc9b177d8b8dce32ae86`
(before/after hash equality), in0.807seconds. All child processes and temporary
fixture data were removed. This is the binary hash, distinct from the backend's
aggregate runtime-source hash.

1. A stale full seed is not ready and cannot serve search by default. `/ready`
   and `/search` return503 while `/index/status` remains readable. An authenticated
   empty replay commit can advance the durable watermark, after which readiness
   and search recover. Bootstrap must wait for index status rather than readiness
   before starting replay. `/health` is liveness, not admission readiness.
2. A partial export is rejected by default even if its own actor count and footer
   hash are internally valid. Full source requires `partial:false` and matching
   source/export counts. Explicit fixture flags exist and must not enter the real
   dev deployment's command line.
3. An oversized searchable projection fails import. A previously committed first
   import batch does not make the index partially ready: `import_complete` stays
   false, `/ready` and `/search` return503. This exercises a real partial-index
   artifact, not only a preflight parser rejection.
4. A65,535-byte opaque avatar URL survives import and actual public hydration.
   Its fallback remains intact and the bounded CDN URL field is null as in the
   native contract. Opaque profile payload bounds are separate from lexical
   projection bounds.
5. An invalid actor inside a multi-actor update batch leaves both prior searchable
   state and the source cursor unchanged. An old actor absent from the snapshot
   cannot be resurrected by an update at/below the snapshot watermark.

The first version of the suite reproduced two real failures: stale snapshots
were considered ready, and partial manifests could be marked import complete.
The backend agent fixed those defaults; the unchanged tests now pass.

## Review of stable core behavior

After the real source exposed an unscopable localhost/port PDS endpoint, optional
source PDS admission changed to preserve the actor and raw metadata while leaving
its canonical scope empty. The invalid-batch atomicity test previously used a bad
PDS as its rejecting row; that is now accepted by design. The rejecting row now
exceeds the unchanged 256-byte normalized display budget. The same no-partial-
mutation and no-cursor-advance assertions remain, and all five adversarial tests
pass. Eight additional source-PDS tests cover retained data, scope exclusion,
strict query scopes, counters, avatar fallback, replay and restart.

Updates are serialized through the writer mutex. Actor validation and document
construction finish before writer mutations. Document changes and the replay
cursor/watermark share the Tantivy commit payload. Only after a durable commit
and reader reload does the engine publish a new `Generation` containing both
Searcher and metadata. Queries capture that generation and hydrate profiles from
that same Searcher, preventing IDs from one view being paired with a later
profile version. Tombstones are stored documents rather than only transient
in-memory suppression, so per-actor version barriers survive restart. The backend
agent's own crash/restart smoke covers pre/postcommit recovery; the independent
suite adds invalid-batch/cursor atomicity and the absent-actor snapshot barrier.

PDS filtering and visibility are part of indexed query eligibility. Exact and
ordinary classes are separately split into preference buckets, so lower-score
local exact matches are not discarded before optional PDS preference. Rank
encoding uses native f32 score order and stable DID order; signed zero is
canonicalized. The query path handles grouped display conjunction inside the
single-token handle/domain alternative rather than flattening mixed/CJK groups
into a union.

CJK contiguity verification happens before a candidate increments the accepted
per-segment top-K count. It uses the same captured Searcher and continues past
false candidates. The backend agent's smoke exercises more than70 false high-rank
bigram candidates before the true low-rank contiguous match. The candidate budget
returns an explicit search-budget error, not an incomplete empty answer.

Public routing omits `/admin/apply`; the separate administrator listener must
bind loopback and requires its configured bearer value. Status/health access
remains available for bootstrap and replay. The existing backend smoke tests
public-write exclusion and missing/bad administrator authentication. This audit
did not expand that into an unrelated penetration-test scope.

## Input and source coverage

The exporter verifies frozen manifest/database/prefix identities and checksums,
walks actor blobs rather than capped prefix postings, preserves native f32 values
and raw profile data, and emits ordered unique actors with a counted/hash-checked
footer. Missing or corrupt footer import never becomes ready. A partial export
is explicitly marked; full dev admission must additionally reconcile exported
count, source manifest count and expected full snapshot count rather than trusting
a caller-created manifest alone.

The source replay uses fixed read-only SELECTs, two `(timestamp,DID)` keysets,
a bounded source cutoff, independent actor/tombstone stream positions, cursor
compare-and-set and a final watermark advance only after both streams drain.
Ambiguous transport acknowledgements re-read the durable cursor before retrying.
A source gap beyond configured tombstone retention fails closed. These are
appropriate correctness structures; real-source query plans and throughput remain
root/source-agent admission work.

Current worker moderation overrides materialize `actors.hidden` and touch
`updated_at`; ordinary source replay therefore carries them. Native overlay
`source=1` rows are documented as future operator overrides. The new actor record
does not represent a separate override priority. If actual live manual source=1
rows exist, their migration/ownership needs an explicit gate; absence of an API
caller in the source tree is not evidence that the production row count is zero.
The root owns that live inventory check.

## Remaining integration limits

- Searchable actor projection remains deliberately bounded. Raw native u16
  payload support does not imply unlimited normalized lexical text. The agreed
  policy is fail closed, do not skip actors or silently switch to handle-only
  indexing. A full import may discover legacy source offenders; preserve a minimal
  receipt of error class and raw field byte lengths, with no raw profile payload
  transferred home, before deciding a broader projection policy.
- Timestamp replay assumes no unbounded backdating or delayed commits behind an
  already drained cutoff. The current overlap catches rows arriving at exactly
  the previous upper-bound second; a late row with an older timestamp can still
  be skipped. Same-second differing actor/tombstone versions fail with an explicit
  conflict rather than silently choosing an order. Source timestamp guarantees,
  overlap/reconciliation policy or an ordered source log need to be documented
  before claiming complete long-lived freshness. This audit flags the limitation
  rather than inventing a new event-source architecture during bootstrap.
- Public HTTP timeout does not cancel a running `spawn_blocking` task. Its owned
  semaphore permit stays inside that task, correctly bounding outstanding search
  jobs. Candidate visitation and segment limits also bound explicit collection.
  Tantivy scorer setup/internal posting traversal is not represented by the
  candidate counter, however; a timeout is not proof the underlying query work
  stopped. Storage-pressure/concurrency qualification must observe this behavior.
- The administrator apply path serializes writes but does not have the public
  search admission semaphore. A trusted replay client's bounded retries limit a
  single attempt sequence; persistent slow commits should not be treated as an
  excuse for unbounded overlapping sync processes.
- PDS filtering is complete for recorded canonical PDS metadata, not a repair for
  missing source identity data. The missing-PDS actor counter and source coverage
  remain meaningful admission evidence.
- The new private administrator API is not the legacy public `/admin/refresh`
  transport path. An eventual production cutover must explicitly account for the
  worker's immediate-refresh integration and its source replay/freshness contract.
  Parallel dev exposure does not itself replace that production path.
- Full-corpus memory, build/merge duration, update progress, queue-inclusive tails
  and same-region public smoke remain measured deployment gates. Passing local
  correctness tests is not a performance qualification.

The critical stale/partial and bounded-input gates exercised here pass. This
supports continuing the explicitly bounded full-corpus dev bootstrap with the
root's live inventory/resource guards; it does not authorize production routing
or waive the remaining source, freshness and workload checks.

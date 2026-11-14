# Tantivy search backend

The search engine each slot runs beside its store process, which feeds it
through the source protocol below. It uses Tantivy 0.27.0 with the repository's
canonical Zig normalization/PDS code through `../tantivy-normalizer`.

## Build and local verification

From the repository root:

```sh
just --no-dotenv test-tantivy
```

Requires Zig 0.17.0, Rust/Cargo, a C compiler, Python 3, Bash and Just. The recipe
builds the canonical host normalizer, runs locked release-profile Rust tests and
actual local HTTP/source/policy/expiry/admission suites, and exercises both
retrieval strategies. Cargo jobs and test threads are bounded at two; no debug
cache is built. Fixtures, receipts and a frozen test binary live in one owned
temporary tree removed on exit. It does not deploy, load credentials or contact
production. The separate [Tangled workflow](../../.tangled/workflows/tantivy.yml)
runs this same command on main pushes; Worker and ingester checks remain
separate.

`TA_NORMALIZER_LIB_DIR` can select a target-specific directory containing
`libtypeahead_normalizer.a`. Do not link a host archive into a Linux binary.
The backend runtime has no dynamic dependency on Zig. `build-receipt.json`
distinguishes runtime source-manifest hashes from native executable hashes.

The real HTTP pilot imports 82 synthetic actors, starts actual listeners, mutates
profiles, restarts processes and aborts them immediately before/after commit.
It checks multiword recall, PDS filtering/preferences, exact priority, f32 ties,
CJK verification/refill, profile hydration, source-version/tombstone durability,
source-cursor atomicity, public write exclusion and failed import readiness.
`TA_BINARY` and `TA_SMOKE_RECEIPT` select a cloud-local binary and receipt. `TMPDIR`
can place its scoped temporary data on an owned cloud scratch volume. Pilot
credentials exist only in child environments and are never printed.
The separate source-PDS suite imports unscopable source values (including a
localhost/port URL), verifies global retention and scope exclusion, checks counter
transitions and restart/replay preservation, and confirms strict query, finite
score and lexical-projection admission remain unchanged. `TA_SOURCE_PDS_RECEIPT`
selects its receipt destination.

## Seed import

The service must be stopped while another process imports into its data directory.
A new initial import refuses an existing index; it never silently resets one.

```sh
typeahead-tantivy import --data /data/tantivy --input - --writer-memory-mb 128 --batch-size 50000
```

Bulk import and `resume-import` accept `--indexing-threads` (1–8) and
`--final-segments` (1–32). Import memory is a total budget, shared across workers,
with at least 15 MiB per worker and a 4 GiB maximum; batches are bounded at one
million records. Defaults remain one worker, 128 MiB, 50,000 records and one final
segment. These options do not change the serving writer. Finalization merges
disjoint groups concurrently when needed to reach the segment bound. The
`import_indexed` and `import_merge_finished` events separate indexing and final
merge timings. Multiple segments require search qualification on the same input
before changing production settings.

`import` builds an index from a JSONL stream. The tests seed their indexes
with it; a slot's index is fed by its store:

1. Manifest: `type=manifest`, `build_id`, `source_watermark`, `actor_count`,
   `source_actor_count`, `partial=false`, and source provenance.
2. Strictly increasing unique UTF-8 DID actor records.
3. Mandatory footer: `type=end`, matching build/count and SHA-256 over the exact
   actor JSONL bytes, including each line's LF.

Full-source import is the default. It requires `partial=false` and equal emitted/
source actor counts. Only explicit fixture imports may pass `--allow-partial true`;
their stored `full_source` remains false. `--build-id` optionally names the candidate
generation; the original source build ID remains separately recorded.

New imports use `tantivy-0.27-f32-did-expiry-v4`. Streaming expiry inspection
keeps aggregate counters, not label samples or actor identities. Coverage records
the exact expected/checked/source actor counts, verified actor-stream footer
SHA-256, canonical moderation rules and Rust classifier fingerprints. It includes
hidden live actors; deleted records count toward the input denominator but have
no live labels to evaluate. Import completion is recorded only after the footer,
count/hash verification and final commit. Failed or interrupted imports retain
incomplete evidence in committed checkpoints.

`expiry_coverage.imported` preserves the initial observations;
`expiry_coverage.current` tracks live documents through subsequent source commits.
Unsupported expiries anywhere in a nonnegated live label array, including labels
from other labelers, and malformed label arrays block readiness globally. They
are not successful empty search answers. Source repairs/deletions can restore
readiness; version-only replays do not change coverage counters. Malformed actors
are reported separately rather than claiming complete label classification.
The private observation protocol and restart retain these aggregates atomically
with documents/cursors.

Older formats remain readable, but their missing coverage is `unknown`, never
inferred zero. They cannot serve through the new readiness gate or qualify for
cutover until evidence is established. A future bounded committed-index audit can
upgrade existing data without full reimport; this change does not implement that
audit or claim one ran. Changed classifier/rule fingerprints likewise require
new evidence. Unrelated binary changes do not invalidate the classifier hash.

Actor fields are `did`, `handle`, `display_name`, `avatar_url`, `labels` (JSON array),
`associated` (JSON object), `created_at`, original `pds`, finite `quality_score` (f32),
`version` (u64) and `hidden`/`deleted`. Optional counters default to zero. JSON-string
labels/associated are accepted and parsed. Original PDS values are retained unchanged in the committed actor payload; a
separate canonical host is used for filters. An optional nonempty source PDS
that cannot be represented by the native DNS-host scope policy receives an empty
canonical host. This includes otherwise well-formed URLs using localhost/ports;
it does not assert that every such URL is malformed. The actor remains in global
search with the same ranking/profile/version. No host is guessed or extracted.
This source-metadata policy does not relax query `pds` or `prefer_pds` validation:
unrepresentable request scopes still return 400. Opaque u16 profile fields
are retained up to 65,535 bytes, including long avatar URLs. Searchable projections
still obey the declared canonical normalizer budgets. An actual projection failure
aborts admission, recording only error class, raw field lengths and accepted count.
There is no silent actor skip or handle-only fallback.

Import retains one actor plus the bounded writer, not a corpus actor/version map.
It commits batches while `import_complete=false`, verifies the footer, merges
segments offline, then atomically commits readiness metadata. Missing/corrupt
footers, duplicate/out-of-order DIDs or normalization failures never admit the
partial generation. Input lines are bounded to 4 MiB. The parent cloud
supervisor owns full-source hash verification, storage budgets and teardown.

## Serve

```sh
typeahead-tantivy serve --data /data/tantivy --listen 0.0.0.0:8080 --admin-listen 127.0.0.1:8081 --concurrency 4 --max-candidates 10000 --writer-memory-mb 128
```

Public GET routes:

- `/search?q=...&limit=10&pds=host`
- `/debug/search`, using the same matching/ranking path
- `/health`: liveness plus truthful readiness, watermark lag and build metadata
- `/ready`: readiness status
- `/index/status` and `/index-status`: committed generation/source/cursor metadata
- `/actors?actor=<did or handle>`, the parameter up to 25 times: the stored
  fields of each live document under their stored names, with `hidden` (the
  moderation decision a search would make now) and `hidden_stored`. In the
  order asked for, each actor once; a handle matches without regard to ASCII
  case. Hidden actors are returned, marked: the caller decides what to show
- `/corpus`: `index`, the index's own counts (`actors`, `searchable`,
  `handle_invalid`, `quarantined`, `watermark`), taken at most once a minute,
  and `store`, the co-located store's last corpus count, or null until it has
  posted one to the admin listener's `/admin/corpus`. The count is kept in
  memory only
- Both canonical and compatibility XRPC actor-search paths, with equal bodies

`/actors` and `/corpus` answer 503 while the index is not ready, as search
does. They are what the Worker reads profiles and the statistics page's corpus
numbers from.
An actor document may carry `description`, `banner_url`, `updated_at` and
`profile_checked`. Each is stored only when set, so a document without them
serializes and hashes as it did before they existed.

Default `/ready` and search require a verified full-source import, matching
normalizer/plan versions, at most 256 segments, and a committed sync watermark no
older than 1,800 seconds (or implausibly future-dated). `/health` and index status
remain readable while stale, allowing the source to advance the cursor.
Do not start sync by waiting for fresh search readiness: read index status first.
Only synthetic/local tests may explicitly use `--allow-partial true` and
`--allow-stale true`. These flags are absent from a real development deployment.
`--max-sync-lag-s` can set the explicit serving lag threshold.
They also require complete matching expiry evidence, covering the current live
actor count with no unsupported or malformed live-label actors. Fixture flags
do not bypass this coverage gate. Index status exposes `expiry_coverage_ready`
and `expiry_coverage_state` (`unknown`, `incomplete`, `blocked`, `complete`).

A hard PDS filter applies before selection, with `X-Typeahead-Pds` acknowledging
the canonical host. Scope policy is `all-recorded-canonical-hosts`, without the old
small-host cutoff; `pds_missing_actor_count` exposes unknown source identities.
`pds_missing_actor_count` means truly empty raw source values.
`pds_invalid_actor_count` means nonempty source values unrepresentable by that
hostname policy, with only two bounded reason categories: `invalid_hostname` and
`too_long` (beyond the maximum 263-byte native hostname/URL wrapper). The stored
reason counters count nondeleted records, including hidden records; live updates,
deletions and restarts preserve their transitions. `pds_unknown_actor_count` sums
both categories. Neither category can match a hard scope or receive a local-PDS
preference. No fallback avatar URL is derived from an empty canonical host;
explicit source avatar URLs retain their existing behavior. Import progress emits
these counters, never raw PDS values or profile payloads. Full-source admission
means all source actors were included, not that all hosting metadata is usable. Optional
`prefer_pds=host&preference=pds_first` or `prefer_pds=host&boost=N` are independent
ranking alternatives; exact handles still have priority.

Admission is fail-fast with four search tasks by default. Query UTF-8 input is
bounded to 512 bytes; canonical normalization/key budgets remain explicit. Limits
are clamped to 1..100. Searches receive a 2.5-second HTTP deadline; timed-out
blocking work retains its admission permit until it actually finishes. The
candidate budget counts scorer-emitted documents, including stale/deleted matches.
Budget/deadline exhaustion returns an explicit 503, never a false empty answer.
Internal Tantivy scorer construction/seek work is not claimed to be an unconditional
constant-time bound.

### Segment merges

Every applied batch commits one segment, and a replaced document stays in its old
segment, marked deleted, until that segment is merged. Both cost queries: each
retrieval pass opens a scorer per segment, and deleted matches are visited and
count against the candidate budget. By default the serving writer keeps Tantivy's
merge policy: a size level merges at eight segments, deletions never cause a merge,
and a segment with more than ten million live documents is never merged again.
That suits a slot that is replaced by a fresh one-segment snapshot twice a day. It
does not suit an index that is only ever updated: on `typeahead-shadow-a` on
2026-11-11, a day after its bootstrap resync, 11.4 million of 24.7 million stored
documents were deleted, in 12 to 18 segments, and nothing was going to merge them.

`serve` takes the policy's settings; unset, each keeps the default above:

| option | default | meaning |
|---|---|---|
| `--merge-min-segments` | 8 | segments in one size level that start a merge of that level |
| `--merge-level-log-size` | 0.75 | log2 of the size ratio between one level and the next |
| `--merge-min-layer-size` | 10000 | segments smaller than this are placed as if they held this many documents, so the smallest level is one segment that absorbs each new commit until it is 2^level-log-size times this size |
| `--merge-delete-ratio` | 1.0 | a level holding a segment with more than this share deleted is merged, whatever its segment count; 1.0 turns that off |
| `--merge-max-docs` | 10000000 | a segment with more live documents than this is left alone |

`--merge-nice N` (0 to 19, Linux only) gives the writer's merge threads that
scheduling niceness, so a merge yields the CPU to a search; unset, they keep the
process's. On `typeahead-shadow-a` during a 13-million-document merge on two
dedicated CPUs (2026-11-11, 400 loopback queries a sample), the 90th percentile
was 4.9 ms with the merge threads at 0 and 3.0 ms at 19, twice.

`/index/status` reports the settings in force as `query_config.merge`, and
`documents` and `deleted_documents`, summed over the generation's segments. Merges
run on Tantivy's merge threads, not under the writer lock; a merged segment is
served from the next commit. `tests/merge_policy.py` runs the shadow slot's
settings against the real server: 3,000 documents, half of them replaced, settle
into at most two segments with under a fifth deleted, across a restart, with every
replaced handle gone from `/search` and every document answering `/admin/source-state`.

### Page-cache warming

A search reads the index through memory maps, so the first search to touch a page
that is not in the page cache waits on the disk for it. After a restart, and after
a large merge replaces most of the index with one new segment, that is every
search. On `typeahead-shadow-a` on 2026-11-11, 25 minutes after a restart with no
traffic but the comparison's 58 queries, 197 of 211 first-time queries took major
page faults (about nine each) and their median was 6.9 ms; the 14 that took none
had a median of 1.5 ms. The faults were in the term dictionary and the postings.

`serve --warm-segment-files term,fast,fieldnorm,idx` reads the files with those
extensions, when they are at least 16 MiB, twice from start to end on a background
thread at niceness 19 (Linux keeps a page read twice ahead of pages read once, and
the store process beside it reads large files once): for the segments open at start, and afterwards for each
segment a commit is the first to bring in. It logs `segment_files_warmed` with the
bytes and seconds. It does not delay readiness or the commit, so searches in the
first minute still fault. Unset, nothing is read. What stays cached is the
kernel's decision: name only as much as the machine's memory can hold beside
everything else that reads files on it.

## Durable incremental replay

Public routes contain no write handler. A separate loopback-only admin listener
starts only when `ADMIN_SECRET` is supplied through the platform's existing secret
surface. There is no remote admin bind option. Its only write API is:

```text
POST http://127.0.0.1:8081/admin/apply
Authorization: Bearer <platform-provided secret>

{
  "actors": ["full actor/tombstone records"],
  "source_cursor": {"the source's own cursor": "..."},
  "expected_source_cursor": null,
  "watermark": 1793236000
}
```

Batches are at most 1,000 actors and 16 MiB. `source_cursor` is an opaque bounded
object, compare-and-set against the previous committed cursor; initial cursor is
null. `watermark` is optional and advances only when the source has drained all
source streams through its safe cutoff. It must not mean merely the largest actor
timestamp seen. Responses report applied/replayed/stale counts, cursor, watermark
and Tantivy commit opstamp. On a slot the store process's index bridge is the
client; this crate does not acquire source credentials.

Every live actor or tombstone has a stored full record and source version in the
same Tantivy document. Higher versions replace; identical replay is a no-op;
stale versions are ignored; conflicting equal versions reject the entire batch.
Snapshot import assigns its coarse source watermark as the initial version floor,
and replay must start strictly after that floor. Source timestamps have second
precision, so a genuinely conflicting same-second source mutation remains an
explicit integration conflict, not an invented total order.

Rejected replay batches retain the public `409 SourceVersionConflict` response.
Stderr distinguishes two bounded events without logging source payloads:

- `source_cursor_conflict`: SHA-256 fingerprints of expected, committed and
  incoming cursors. The fingerprint hashes the compact serialization of the
  parsed JSON value, with the normal deterministic object-key ordering.
- `actor_equal_version_conflict`: SHA-256 of the DID UTF-8 bytes, stored/incoming
  versions, old/incoming deletion flags and a fixed whitelist of differing
  actor field names. Values, raw DIDs, cursors, profiles and credentials are
  never included.

These events diagnose which existing guard rejected a batch. They do not change
conflict policy, resolve equal timestamps, advance a failed cursor or add an
operator write endpoint. The focused actual-HTTP test verifies both classes,
redaction/bounded output, unchanged atomic rejection and valid identical replay
across restart. `TA_CONFLICT_DIAGNOSTICS_RECEIPT` selects its receipt destination.

Documents and source-cursor/build metadata commit atomically. Validation finishes
before writer mutation. Storage failures roll back pending work and/or poison
readiness; they do not allow an uncertain cursor to continue. Tombstones remain
stored but unsearchable, preserving barriers after restart. A single captured
Searcher supplies both candidates and stored profile payloads, preventing mixed
generation hydration during concurrent writes. SIGTERM/SIGINT requests graceful
shutdown; committed generations survive abrupt process loss.

`serve` starts the merges the previous process left pending before it listens,
with a commit that carries the committed payload unchanged. Without it, a
process that restarted with eight or more mergeable segments started that merge
during its first write, while the write's delete was still uncommitted, and the
merged segment brought the replaced document back: `/search` served the old
handle beside the new one and `/admin/source-state` answered 500 for that DID.
`tests/restart_merge.py` reproduces it (8 of 11 runs before the change, measured
locally on 2026-11-10) and is part of `just --no-dotenv test-tantivy`. The change
is local and tested; it is in no deployed binary. Why Tantivy's own catch-up at
the end of that merge does not remove the document is not established.

## Source-owned observations and generation policy

The private `/admin/source-state`, `/admin/source-page` and
`/admin/source-reconcile` routes are the source protocol the store's index bridge speaks. Each
reconciliation batch checks expected document hashes, the prior opaque cursor
and an independent observation revision before committing. Identical batch-ID
retries recover the durable receipt; changed-body reuse and stale preconditions
reject atomically. Generic `/admin/apply` is fenced after this migration. Source-authoritative
observations that differ only in version retain the stored document/version and
count as replayed while advancing durable observation metadata. That stored
version can remain a coarse seed or last projection timestamp; it does not order
source observations. All other validated Actor fields remain part of equality.
 Source
Unix seconds remain evidence rather than an invented total order.

`/admin/source-state` answers `{did, document_sha256}` per requested DID. With
`"fields": true` in the request it also returns a read-only `fields` object for
each stored document (null when absent): `deleted`, `hidden`, `handle`, `pds`,
`pds_host`, `quality_score`, `version` and `profile_sha256`, a SHA-256 over the
compact JSON array of display name, avatar, labels, associated and creation time.
The document hash covers the source version, so two backends fed by different
sources cannot be compared by it; these fields let the
[shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md) name what differs.
The default response and the reconcile protocol are unchanged.

An optional complete `policy_snapshot` carries operator DID actions, blocked
domains and its source observation clock. It shares the same commit, cursor,
revision and retry receipt as profiles. Source clocks cannot move backward or
more than 300 seconds into the future. Omitting the snapshot preserves the
existing policy without refreshing it. A cached validated policy belongs to each
captured generation; canonical moderation runs before top-K acceptance and
refills past rejected candidates. It can rescue an old materialized hidden
profile through a show override. CJK verification uses that same hydrated actor,
and accepted profiles are reused for output. Quarantined identity claims remain
excluded independently. Unsupported label expiry returns 503 rather than a
successful empty response.

Legacy policy-enabled generations use `tantivy-0.27-f32-did-policy-v3`, which
source-only binaries refuse. New imports and their later source/policy commits
retain `tantivy-0.27-f32-did-expiry-v4`; older binaries refuse this coverage format.
New binaries can read legacy v1/source v2/policy v3 metadata but cannot infer
missing expiry evidence or serve those generations as qualified. Policy freshness is limited to
300 seconds even with the fixture `--allow-stale` flag. `/index/status` exposes
policy hash, clock, age, counts and canonical rules source hash; it never exposes
the policy's rule/DID lists or internal retry receipt.

`services/tantivy/tests/source_policy.py` (part of `just test-tantivy`) drives the
real binary through policy expiry and readiness, refused snapshots, policy-only
batches that change eligibility before the top results are cut, and a snapshot
committing with its batch at both kill points.
hydration time and moderation time separately for controlled qualification.

## Retrieval/storage contract and remaining gates

Finite f32 scores are encoded by sortable IEEE bits, with signed zero canonicalized,
then DID in a string sort key. Complete prepared memberships, dedicated PDS and
visibility fields, and engine-managed flush/merge sorting support early top-K
enumeration. Exact and ordinary/local/non-local streams are merged with the full
ranking contract. CJK bigrams are only a candidate accelerator: contiguous-run
verification occurs before accepting each candidate, refilling beyond any fixed
50-result cap until K valid results or explicit work-budget exhaustion.

Stored payload/version atomicity means presentation/version-only document changes
currently reindex unchanged prefixes too. The dependency does not remove write
amplification automatically. Profile hydration, real mixed update/merge I/O,
full-corpus admission, freshness and final 1 GiB serving behavior must be measured
on the isolated deployment before any cutover. The Worker identity-containment
layer remains outside this backend; no production transport or public Worker
configuration is changed by this crate.

## Experimental segment rank merge

`serve --retrieval global-merge` opts into a k-way rank-head merge, stopping after
K eligible profiles per exact/preference bucket across all segments. Default
`per-segment` keeps the baseline collection path. Both apply policy and CJK
eligibility before acceptance, and both use exact mathematical f32 base-plus-boost
comparison. Debug fields distinguish scorer setup, rank-head reads, document
visits, hydrated profiles and unique segments; they are not disk/posting counters.
No full-corpus speed claim follows from those fixtures.

Successful public responses include `X-Typeahead-Engine`, `X-Typeahead-Build`,
`X-Typeahead-Format`, `X-Typeahead-Binary-Sha256` and
`X-Typeahead-Retrieval-Strategy`. The binary digest is computed from the actual
executable at process startup, not from a release manifest. Index status exposes
that digest, strategy and query configuration for admission checks. Search build
and format headers belong to the generation that produced the response.
Explicitly present empty or invalid `pds`/`prefer_pds` values return 400 on all
search aliases, rather than becoming an unscoped search.

At startup, bounded ASCII `FLY_MACHINE_ID` and `FLY_MACHINE_VERSION` values become
status `machine_id`/`instance_id` and successful-response
`X-Typeahead-Machine-Id`/`X-Typeahead-Machine-Version` headers. Missing values are
null locally; malformed present values prevent startup. No CLI or release-file
identity override exists. Real Fly admission must compare these runtime values
with provider machine/instance identity and reject configurations that override
the reserved platform variables.

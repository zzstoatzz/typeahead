# Tantivy dev input and source replay

Owner: tantivy-dev-20261028. Source exporter: `services/tantivy-input/export.py`.
All real artifacts and corpus bytes remain on the isolated cloud VM. The exporter
performs no networking and reads no credentials. Root owns credentials, resource
leases, data download/fork, deployment, disk budget and teardown.

## Local source-observation successor

The local branch now defaults to source-owned observations, with independent
observation revisions and bounded identity reconciliation. See
[source-observations.md](source-observations.md) and
[the source-adapter README](../../../services/tantivy-input/README.md).
The timestamp replay and retained-tombstone sections below describe the preceding
policy; their deployment receipts do not qualify this successor's runtime or
full-corpus sweep SLO.

## Immutable import

Read `manifest.json`, the small compact `index.db`, and `index.db.prefix` from a
new independent cloud copy or cloud-only R2 capability. Never run against the
serving or rollback machines or send the payload through a laptop/heavypad.
Use the generation's actual build ID and an existing declared scratch directory:

```sh
set -o pipefail
python3 /app/input/export.py \
  --manifest /data/source/manifest.json \
  --database /data/source/index.db \
  --prefix /data/source/index.db.prefix \
  --build-id BUILD_ID --scratch-root /data/import \
  --output - --receipt /data/import/export-receipt.json \
  | /app/typeahead-tantivy import --data /data/tantivy \
      --input - --build-id BUILD_ID
```

Wrap the entire pipeline in the cloud process supervisor's hard timeout. The
exporter CLI also installs a Unix wall alarm (including blocked stdout), while
its importable function uses cooperative checks. Root
must account for source plus index disk usage and ensure cleanup independent of
a local chat process. Input defaults: <=8 GiB source artifacts, <=16 GiB emitted
JSONL, <=128 MiB root directory, <=16 MiB one data extent, <=20 million actors,
30-minute cooperative deadline. Overrides must stay within the cloud lease.

The exporter verifies both complete file SHA256s before emitting data, closed
immutable SQLite metadata/checksums, exact release/build/normalization/scoring
identity, FLATPX05 header/root/extent bounds, all visited actor-page restarts,
posting boundaries, actor ordering/uniqueness, UTF-8, packed version/f32 and
hydration JSON shapes, final actor count, and unchanged source file identities.
Actor entries come from the complete tag-1 namespace, never capped prefix lists.
The root directory plus one extent bound memory; no actor dictionary is retained.

JSONL is `manifest`, strictly DID-byte-ordered `actor` records, then mandatory
`end`. The manifest includes build ID, numeric `source_watermark`, string
`watermark`, source/target actor counts, `normalize_version`, `scoring_version`,
source hashes and explicit `partial`. Each actor preserves did, handle,
display_name, avatar_url, raw PDS URL, labels as array, associated as object,
created_at, hidden and exact finite packed-f32 quality_score (plus diagnostic
f32 bits). Native actor output does not expose follower/post counts; compact
hydration sets these to zero. PDS canonicalization for filtering must retain the
raw URL separately for avatar fallback. Base records set deleted=false.

The end record is `{type:"end",build_id,actor_count,actors_sha256}`. Its hash is
SHA256 of exactly emitted actor JSONL UTF-8 bytes INCLUDING each trailing LF,
excluding manifest/end. The Rust importer must verify footer, count and hash and
keep `import_complete=false` until they match; partial commits are never ready.
`--limit-actors N` is an explicitly partial operational pilot, not a full corpus
or production qualification. File output and receipt use new `.partial` files and atomic no-clobber hard-link
publication; failed exports remove their own staged/final files. Stream failures omit `end`.

## Version fidelity

Packed actor-v1 has no per-actor source timestamp. Base records therefore use
`version=source_watermark`, `version_kind="snapshot_watermark"`, and
`source_updated_at=null`; this is a coarse snapshot barrier, not an invented
individual update time. Source `updated_at`/`deleted_at` values are Unix seconds.
Native snapshots establish this cutoff, and current native sync replays strictly
newer rows. Equal-version conflicting payloads must fail explicitly, not become
last-writer-wins. Same-second source mutations are not distinguishable by the
existing source timestamps and remain a source-protocol limitation.

## Read-only replay

`sync.py` reads only fixed,
parameterized SELECT statements over the Turso HTTPS `/v2/pipeline` protocol.
The cloud-only `TURSO_URL`/`TURSO_AUTH_TOKEN` belong to root's scoped read-only
credential setup; no values enter arguments, logs, receipts or local files.
Actor and tombstone streams use `(timestamp,did)` keysets, an explicit indexed
`timestamp >= cursor` anchor, a strict snapshot floor and a safe source-clock
cutoff. Actors with cleared handles become versioned deletions. Tombstones and
hidden/rename/rescore/PDS changes preserve their source version barriers. Each
tombstone page uses one bounded indexed LEFT JOIN to the current actors table.
A retained tombstone is superseded only when the same SQL read sees a nonempty
current handle with `actor.updated_at >= tombstone.deleted_at` AND
`actor.updated_at <= cycle.upper_bound`. Such tombstones are not submitted to
Tantivy, but the raw tombstone keyset still advances. Hidden current actors stay
hidden; cleared handles, missing actors and older live rows still produce
canonical versioned deletions. A future restoration beyond this cycle's cutoff
cannot suppress a deletion before the next eligible cycle.
A source cursor advances only with the same Tantivy commit as its actor batch;
CAS prevents competing replay workers from losing progress. A shared
caught-up watermark advances only after both timestamp streams drain to the
cutoff. At every new cycle, both keysets rewind by the persisted
`lookback_seconds` policy (default 300), bounded below by the strict snapshot
floor. This catches ordinary late commits within that overlap; identical source
versions are replay acknowledgements and do not reindex actor documents.
Requests, source bytes, pages and runtime remain bounded. A long initial
catch-up can pause with committed progress and resume next cycle; `--once`
returning successfully with `caught_up:false` is not a freshness admission.

Tombstones are retained seven days upstream. A snapshot/replay gap beyond that
requires a fresh snapshot, not a claim of complete recovery. Source clock coarseness, backdating beyond the bounded lookback, and local-only
operator overrides remain explicit limitations. Equal-version conflicting
payloads (including ambiguous same-second delete/restore) fail closed rather
than invent an order. The timestamp view is not a change journal or a proof
against arbitrarily late/backdated source transactions. Initial snapshot count excludes hidden/unsearchable actors
by builder design; subsequent source changes require live replay before a
freshness claim is made.

## Verification

`PYTHONDONTWRITEBYTECODE=1 python3 services/tantivy-input/test_export.py` passes
9 tests using the repository's actual FLATPX05 reference writer. Coverage includes
restart groups and oversized actor extent, Unicode/hydration/f32 precision,
manifest/build/checksum/truncation failure, forged-valid-digest structural
corruption, forged counts, explicit partial export, output budget failure and
non-overwrite/cleanup behavior. These are synthetic local inputs, not downloaded
corpus data. Root documentation remains responsible for measured cloud state.


Replay invocation (credentials are read from environment only):

```sh
python3 /app/input/sync.py --admin-url http://127.0.0.1:8081 \
  --status-url http://127.0.0.1:8080/index/status \
  --lookback-seconds 300 --page-size 500 --max-pages 256 \
  --max-cycle-seconds 300 --max-run-seconds 3600 \
  --status-file /data/import/source-sync-status.json
```

Default source-response budget is 512 MiB for the bounded process lifetime,
with 4 MiB per response and finite HTTP/retry/backoff limits. Source SELECTs
include cleared handles in the actor stream rather than using an unbounded
separate cleared-handle scan. The source URL requires HTTPS, redirects are
prohibited, and admin/status destinations must be HTTP loopback. No SQL writes
or user-supplied SQL are allowed. Durable actor/cursor transactions live in
Tantivy; the optional atomic status file is observational, never authoritative.
Reported `records` counts raw source rows, `submitted_records` counts actor
records sent to the backend, and `superseded_tombstones` counts retained
historical deletes omitted by the current-state join. `applied`, `replayed`,
`stale` and `ack_recoveries` distinguish backend logical work. They are not WAL or physical membership-write measurements.

`test_sync.py` passes 15 real-HTTP/SQLite tests: actual read-only source SQL and
index plans, same-second keyset pagination, clear/delete/version behavior,
commit failure without cursor advance, lost acknowledgements, CAS, bounded
pause/resume, 300-second late-commit recovery without reindexing, and retention
failure. `test_rust.py` additionally runs the actual compiled Rust backend with
synthetic source HTTP/SQLite, verifies unknown actor admission, cleared handles,
tombstones, cursor persistence across process restart, unchanged membership
counters on replay, and a late source row recovered within the lookback. No
real Turso/cloud endpoint is used by these tests.

## Small cloud operational pilot

`fixture.py` emits eight synthetic actor records, a matching manifest/footer,
and expected query/hydration cases. `qualify.py` targets only HTTP loopback and
refuses a backend without the matching `fixture-*` build identity. Its optional
`--mutate --input FILE --version-start N` mode exercises only these synthetic
records: hide/show, equal replay, stale restoration/deletion, PDS migration and
rescore, then restores initial query behavior. With the actual local Rust binary,
21 HTTP checks and 10 admin mutations passed. This is operational qualification,
not corpus-scale capacity or a substitute for full real-data import. Explicit
`--allow-partial true --allow-stale true` serve flags may be needed for this
synthetic pilot; real dev serving must use neither override. Restart behavior is
covered separately by `test_rust.py`.


## Retained-tombstone source policy

The durable cursor records `source_policy="turso-current-state-cutoff-v1"`.
Legacy cursors without that field can upgrade in the next atomic commit while
preserving their exact prior opaque value in `expected_source_cursor`; unknown
policy IDs fail explicitly. Backend equal-version conflict checks and cursor
CAS remain unchanged. This policy resolves source history from current source
state, rather than relaxing the engine into last-writer-wins.

Source deletion writers batch `DELETE actors` with `INSERT OR REPLACE
 tombstones` transactionally (`src/handlers/admin.ts`, `src/cron.ts`). Ingestion
can recreate an actor without removing its retained tombstone. Thus an eligible
current live row sharing the tombstone's Unix second is evidence of a later
restoration/current state, not a persistent ban. The current actor row is
projected by the actor stream; the join prevents an obsolete retained delete
from subsequently contradicting it. The additional `<= cycle.upper_bound`
guard preserves the declared cutoff: old backend version 90, tombstone 100 and
future current actor 110 with cutoff 105 must delete now, then restore at a
later cutoff that includes 110.

Tests cover stable live ties, newer live rows, hidden rows, cleared handles,
absent actors, older live rows, all-superseded pages advancing the raw cursor,
bounded resume, and legacy policy upgrade. Actual Rust integration also proves
these cases, cursor persistence/replay without reindexing, and preservation of
strict 409 behavior for an equal-version restoration that arrives only AFTER
its deletion was already applied. These remain ambiguous under second-resolution
source versions and require a stronger source change/version contract to resolve.

The actor stream and later tombstone pages are independent source read snapshots.
The join is coherent within its individual SQL snapshot, not a linearizable
snapshot across the whole multi-request cycle. A late row committed after the
actor stream drained can wait until the next 300-second overlap replay. The
bounded lookback does not recover arbitrarily late/backdated changes. No source
writes, source policy changes to moderation, or backend conflict-rule relaxation
are part of this fix.

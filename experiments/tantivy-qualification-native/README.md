# Isolated native frozen-feed writer

This executable drives the existing native `sync.projectRow` and
`sync.projectRemoval` projection code. It has no HTTP listener, Turso client,
source credentials, production route, or background sync. The only production
module change is making the existing removal projection public. A separate
build target and small module bridge expose existing code to this experiment;
ordinary native server routes are unchanged.

```sh
cd services
zig build qualification-native eval-server -Doptimize=ReleaseSafe --prefix ../experiments/tantivy-qualification-native/.tools
zig build -Doptimize=ReleaseSafe --prefix ../experiments/tantivy-qualification-native/.tools
cd ..
python3 experiments/tantivy-qualification-native/pilot.py
```

The pilot uses a tiny actual native snapshot build, real native loopback search,
and SQLite. Compilers must be supervised with a 360-second subprocess deadline.
The passing pilot checks rename, hide/show, PDS migration, rescore ordering,
stale/replay/conflict/CAS, tombstones, unknown actors, deferred-FK failure at
COMMIT, durable restart and isolation guards against an independent simple raw-
profile oracle. Every successful batch is checked against `fixture-feed.json`;
its exact SHA and binary hashes are retained in `results.json`. This fixture
is deliberately small and ASCII; it does not qualify production recall or speed.

Pilot scratch has a 250 MiB payload budget and 180-second wall deadline; temporary
payloads and child processes are removed on exit. Compiler caches/tool binaries
are separate. `results.json`, when present, is local synthetic evidence only.

## Admitted snapshot startup

`TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT=1` opens an existing verified prefix admission
cache. Missing, invalid, or stale admission fails closed. An unset flag or any
other value retains full snapshot validation. This flag only changes the isolated
writer; production serving and the native evaluator are unchanged.

`test_admitted_startup.py` runs a real Linux writer against a synthetic 60 MiB
FLATPX05 fixture. It checks admission failures, committed mutation, and process
`rchar` evidence that admitted startup avoids a full prefix read. The small
[receipt](admitted-startup.json) records the binary hash and cleaned scratch.

## Private file-inbox protocol

Provision a fresh independent cloud directory, mode 0700, containing an `owner`
file with exact bytes `native-qualification-v1`. Set:

- `NATIVE_QUAL_ROOT`: absolute private directory, no trailing slash.
- `LOCAL_DB_PATH`, `TYPEAHEAD_EVAL_SNAPSHOT`, `NATIVE_QUAL_REQUEST`,
  `NATIVE_QUAL_RESPONSE`: paths under that directory.
- `NATIVE_QUAL_FEED_SHA256`: hash of the immutable frozen feed established by the
  parent manifest/controller; the executable binds batches to that identity.
- `NATIVE_QUAL_SECONDS`: finite process lifetime, maximum/default 900 seconds.
  Parent supervisor must also enforce a process-group deadline and teardown.
- `SQLITE_TMPDIR`: private scratch directory. Optional `NATIVE_QUAL_ONESHOT`
  exits after one acknowledgement, intended for tiny diagnostics only.

When Fly identifies the app, its name must start with `typeahead-qual-`.
Never masquerade as a production app. The parent provider controller must verify
machine IDs, guest shape, volume ownership and the immutable R2 source; a local
marker is an accidental-access guard, not independent provider attestation.

Start the process once, then atomically replace the request file with a complete
JSON batch. It loads the snapshot once and polls the inbox without revalidating a
multi-gigabyte artifact per batch. Poll until the input disappears, then read the
response. It writes and syncs a temporary response, renames it, then deletes the
request. Only one producer may own the inbox. Keep requests and responses in the
cloud VM, not over a laptop data path. The parent can expose a separate private
loopback control adapter, but must not alter public native routes.

```json
{"feed_sha256":"64 hex characters","expected_cursor":0,"cursor":1,"actors":[{"did":"did:fixture:a","handle":"alice.test","display_name":"Alice","quality_score":1.5,"version":1793255000,"hidden":false,"deleted":false}]}
```

Actor fields are `did`, `handle`, `display_name`, `avatar_url`, `labels`,
`associated`, `pds`, `created_at`, `quality_score`, `hidden`, `deleted`, `version`.
Labels and associated are serialized JSON strings for the native packer; convert
from the common feed's JSON values deterministically outside the timed writer.
Scores become finite f32. Versions are positive signed-64 source seconds. Optional
presentation fields default to empty/native defaults. Tombstones use
`deleted:true`; they need no nonempty handle. Unknown actors/terms use the same
normal projection path.

Each batch has at most 1,000 actors, 4 MiB encoded input and unique DIDs within the
batch. The same DID may recur in later batches. This restriction matters because
production old-row lookup uses separate read leases, which cannot see another
uncommitted update to the same actor in the current batch. The common feed must
use the same transaction partition for Tantivy, or explicitly report a difference.
There is no generated looping write workload or implicit prefill.

`BEGIN IMMEDIATE` serializes cursor validation, version checks, ordinary overlay
writes and durable `sync_meta.qualification_cursor` update. Cursor increments by
one; mismatches fail closed. The per-actor version/hash table is committed in the
same transaction. Lower versions are stale, identical equal versions replay,
equal-version differences fail. Snapshot watermark is the initial coarse barrier;
this does not invent absent per-actor snapshot versions. A response is accepted
only after `COMMIT` succeeds. Any projection or commit error rolls back the whole
batch, including cursor and version state. SQL errors are not swallowed.

The last raw request SHA is stored with the cursor. Identical retry of the last
acknowledged batch returns accepted with zero transactions; changed bytes at that
cursor fail CAS. Retrying after a lost response or process restart therefore
cannot repeat writes. Earlier batches are rejected by the strict cursor. Actor
replays in a new correctly ordered batch consume that batch cursor but do not
project memberships.

Receipts distinguish `applied`, `stale`, `replayed`, `transactions` and
`sqlite_changes`. SQLite changes include overlay plus qualification bookkeeping;
they are neither physical bytes nor an assertion that all writes touched lexical
memberships. Record WAL/block writes and commit latency independently. Actor
version bookkeeping is additional evaluator overhead and must remain visible in
any comparison.

The parent must independently check expected query responses at mutation
checkpoints, final effective actor-state digest, feed completion and restart.
The paired timing harness deliberately does not turn a successful file-inbox
receipt into a complete source/readiness qualification.

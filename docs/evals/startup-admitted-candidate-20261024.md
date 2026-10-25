# Verified startup and parallel search candidate

This follows the [metadata-only experiment](startup-buffer-20261024.md). Production
continues serving on `typeahead-search-compact` machine `86e502fe55e4e8`, four shared
CPUs and 2 GiB RAM, with its original image. Routing has not changed.

## Verification and recovery

The experimental bypass has been removed. Installation now performs the complete
SHA-256 and structural verification, computes SHA-256 for each 4 KiB block, and
atomically publishes a synced `.prefix.admitted` sidecar. Its header binds the
expected full digest, inode, size, modification time, and change time of the exact
opened prefix file. Generation-directory publication preserves that identity.

Compact recovery requires that verification record. Missing or stale records fail
closed; recovery does not silently re-admit a compact generation. It maps the
directory and checksums without prefaulting the entire files. Directory blocks are
verified when accessed, once per read lease, and page payloads are checked before
decoding. A new lease revalidates directory blocks. The existing payload cache
holds copies of bytes, whose checksums are still checked when used.

`admit-prefix` provides explicit offline admission through
`TYPEAHEAD_ADMIT_PREFIX_PATH` and `TYPEAHEAD_ADMIT_PREFIX_SHA256`. New-generation
promotion performs admission before semantic probes and activation. Recovery uses
the previously admitted generation and does not repeat those corpus probes.
Legacy-format fallback retains its existing verification requirements.

`/ready` returns 200 only after a snapshot is attached, otherwise 503. `/health`
continues providing detailed readiness and overlay-lag information.

## Parallel candidate preparation

The existing disposable app `typeahead-startup-20261024`, machine
`7841e35f965528`, and volume `vol_v8eyllqdyykq0z5v` are reused. The full current
snapshot is restored at
`/data/candidate/index/generations/b1792484789-3d65-px05/index.db`:
12,641,735 actors, 145,935,146 prefix keys, and a 7,203,934,208-byte prefix file.
Its digest is `2967d49389995b398aabefd2dbf8c9e299e70d11e5c5c3909b6e28cf3ea303e5`.
Only missing download ranges were fetched; existing data was reused.

A consistent online SQLite backup seeds the full 3,552,636,928-byte overlay.
The transfer uses authenticated HTTP over Fly's private network, without relaying
the corpus through the operator's Mac. The temporary production backup and server
must be removed after transfer. Credentials are staged only on the candidate app
and compared with production by hash. The current R2 pointer matches production.

The lab temporarily uses four shared CPUs during bulk preparation. Fly's
[documented volume limits](https://docs.fly.io/volumes/overview#volume-limits)
are 16 MiB/s for two shared CPUs and 32 MiB/s for four. Final qualification must
run at two shared CPUs and 1 GiB RAM.

## Evidence and outstanding gates

Mac native tests pass, including inode replacement, changed content, corrupt and
truncated verification records, failed admission, directory publication, and
read-lease proof invalidation. The eight real Linux flat-index tests also passed
before the per-lease hashing optimization; the optimized build must repeat them.

With the complete snapshot and an empty overlay, the protected Linux reader became
ready in 19 and 16.5 ms on the preparation VM. Removing its verification record
produced `SnapshotNotAdmitted` in 12.2 ms; restoring the record restored service.
These are preflight results, not qualification of the full overlay or smaller VM.

Before cutover, require the final committed image, optimized Linux tests, full
snapshot and overlay integrity, paired global and PDS queries against the frozen
reference, repeated sub-second recovery at two CPUs/1 GiB, bounded concurrent
queries without errors, and healthy live overlay catch-up. Keep the original
production backend available throughout qualification and cutover.

Routing also requires adding the candidate origin to the search transport's exact
allowlist and changing the front door's `SEARCH_BACKEND_URL`. Neither routing
change has been deployed. Rollback restores the original backend URL; do not
remove the original machine or volume before the candidate is accepted.

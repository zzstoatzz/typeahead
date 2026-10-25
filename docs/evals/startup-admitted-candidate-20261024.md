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
were removed after transfer; their absence was checked. Credentials are staged only on the candidate app
and compared with production by hash. The current R2 pointer matches production.

The lab temporarily uses four shared CPUs during bulk preparation. Fly's
[documented volume limits](https://docs.fly.io/volumes/overview#volume-limits)
are 16 MiB/s for two shared CPUs and 32 MiB/s for four. Final qualification must
run at two shared CPUs and 1 GiB RAM.

## Evidence and outstanding gates

Mac native tests pass, including inode replacement, changed content, corrupt and
truncated verification records, failed admission, directory publication, and
read-lease proof invalidation. The final optimized build also passes all eight real Linux flat-index tests.
The complete Bun suite passes 93 tests; the native suite passes 147 tests with
four pre-existing skips.

With the complete snapshot and an empty overlay, the protected Linux reader became
ready in 19 and 16.5 ms on the preparation VM. Removing its verification record
produced `SnapshotNotAdmitted` in 12.2 ms; restoring the record restored service.
These are preflight results, not qualification of the full overlay or smaller VM.

Full snapshot and frozen production-overlay qualification passes at two shared
CPUs and 1 GiB. The unmodified reader takes 213.833 seconds to attach. The admitted
reader takes 18.5, 11.7, and 16.9 ms across cold process starts. All 184 candidate
responses match the reference exactly, including global and PDS-scoped queries.
The cold matrix has median 77.634 ms and maximum 554.667 ms; the warm matrix has
median 13.337 ms and maximum 20.555 ms. Eight concurrent readers complete 24
requests without errors, maximum 129.052 ms. Missing admission records fail closed.

The production-role process also attaches in 14–24 ms with real configuration.
A separate cold diagnostic `/index/status` request takes 4.21 seconds counting
399,173 overlay rows on disk; `/ready` remains fast. That diagnostic is not the
readiness probe. Live catch-up and HTTPS qualification have now passed: 2,805 updated actors,
161 made unsearchable, and 168 deletions were applied, with zero sync lag at the
end of the pass. Global and scoped searches returned 200 in 95–125 ms. The
prepared transport code also reaches the candidate successfully.

The immutable candidate image is
`registry.fly.io/typeahead-startup-20261024@sha256:61b252cbb7f6bef9ec00579083b464622928da91789c3531a13fda96a365e837`,
source revision `f7dfca7cb6e94a3a86557003fe19de9c47a3e17e`. The candidate volume is
60 GiB to allow normal subsequent generation promotion. Snapshots are disabled on
this disposable copy. Keep the original production backend available throughout
qualification and cutover.

Routing also requires adding the candidate origin to the search transport's exact
allowlist and changing the front door's `SEARCH_BACKEND_URL`. Neither routing
change has been deployed. Rollback restores the original backend URL; do not
remove the original machine or volume before the candidate is accepted.

## Serving candidate and cutover

The candidate is live at `https://typeahead-startup-20261024.fly.dev`, running the
immutable image above on two shared CPUs and 1 GiB, with `/ready` as the Fly health
check. A clean VM restart after live writes attached the snapshot 55.3 ms after
the process launch message, including opening the overlay; Fly VM restart itself
also includes platform boot time. Post-restart health, snapshot identity and
both query modes pass. The three post-restart HTTPS searches took 147–955 ms.

An earlier restart after forcibly terminating an in-flight sync spent about five
seconds opening SQLite before attachment. The clean restart did not repeat that
cost. These measurements do not promise sub-second recovery from every unclean
SQLite shutdown; they establish that normal snapshot recovery no longer scans
7.2 GB of index content. A cold `/index/status` remains a costly overlay count
and must not replace `/ready` as the readiness probe.

Evidence: [frozen comparison](startup-full-candidate-frozen-20261024.json),
[artifact hashes](startup-full-build-20261024.json),
[live catch-up](startup-full-live-serving-20261024.json), and
[clean restart](startup-full-restart-20261024.json).

Production routing still points at the original backend. Cutover requires deploying
only the search transport allowlist addition, then changing `SEARCH_BACKEND_URL`
to the candidate origin through the existing configuration workflow. Preserve
unrelated Worker changes. Smoke-test global and PDS-scoped requests through the
public front door, then inspect latency, errors and overlay lag. Immediate rollback
restores `https://typeahead-search-compact.fly.dev`; keep its machine and volume
until the candidate is accepted. The new admission scheme cannot recover a compact
volume that lacks its admission record without explicit offline admission.

The old derived subset fixtures and staged test binaries have been deleted. The
full verified snapshot, live overlay, immutable image and qualification evidence
remain on the single candidate machine and volume. No production transfer server
or backup remains. The parallel resources are intentionally retained for cutover;
retire the rejected backend and its volume after acceptance to end duplicate cost.

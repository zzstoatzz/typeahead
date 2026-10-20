# PDS homepage review — October 20, 2026

PDS is not deployed; the authorized rollout is in isolated qualification.
The initial preview review made no production deployments, publisher changes,
or new hosted resources. Ported the October 8 PDS experiment onto current compact main
`369cf6a`, preserving the primary checkout's unrelated edits.

The filter uses the existing homepage colors, typography, borders and button
sizes. A 44px control sits inside the existing 48px search field. Its anchored
panel takes no layout space while closed (or open). Apply normalizes the hostname,
adds an active dot and accessible host label, saves scope in the URL and searches.
All servers clears scope. Escape cancels edits; reload and history restore scope.
The active button tries the selected host’s HTTPS `/favicon.svg`, then
`/favicon.ico`, with no referrer, and retains the sliders if both fail. The icon
is decorative, 18px, with a subtle active tint; the control remains 44px. No
backend proxy, third-party icon service or search-path work is added. Removed
images cannot replace the icon for a newly selected scope.
The browser and Worker share the hostname normalizer. Superseded searches are
aborted and guarded by a generation counter. Unavailable scopes and empty results
are separate states; invalid scope URLs cannot trigger global searches.

## Verification

- TypeScript check, targeted Oxlint, 98 Bun tests and Zig test targets passed.
  ReleaseFast service and loopback evaluation-server builds passed.
- Real FLATPX05 conversion retained scope metadata and registry. The candidate,
  PDS-only reference and current-main engine ran against compact files built from
  the same 100,005-actor synthetic source. 1,712 response comparisons passed.
- 18 real workerd/Miniflare checks passed: normalized scope caches, global/scope
  isolation, both aliases, excluded/invalid/unknown scopes, unsupported and dead
  engines, and emergency proxy mode. Scoped requests failed closed.
- Real admin refresh exercised membership migration, hiding, renaming and exclusion;
  removing the policy-ready marker disabled scoped search while global stayed up.
  Zig tests cover deletion and policy hashes. This does not test full remote replay.
- Local unscoped p50/p95: current main 0.211/0.537 ms, candidate 0.211/0.522 ms.
  Scoped p50/p95: 0.231/0.575 ms. Single warm fixture run, not a production or
  statistical no-regression claim. Full compact artifacts: 37,863,424 bytes versus
  35,098,624 bytes with scopes disabled (+7.9%).
- Browser checked desktop 1280px, mobile 390px and narrow 320px; light/dark, apply,
  clear, Enter, Escape, invalid input, URL reload/history, populated, empty and
  unavailable states. At 390px the field stayed y=231.336, h=48; the following
  form stayed y=338.531, h=48 across panel open/close. No horizontal overflow;
  browser console read returned no warnings/errors. The later deterministic race
  check held a real scoped `bob` response until `person` results were rendered,
  then released it: the newer results remained. A local-only 200% CSS zoom
  check at 1280px kept the open panel within the viewport without horizontal
  overflow. This is CSS zoom coverage, not a claim of browser-menu zoom testing.
- Read-only production health: compact ready, expected build b1792400507-4df8-px05,
  lag 263 seconds at this check. The public freshness route returned 403 without
  authentication. No claim of live PDS verification: it is not deployed.

[Machine-readable fixture evidence](pds-compact-results-20261020.json).
Local artifacts and runnable preview are under
`~/.local/share/typeahead-experiments/pds-homepage-20261020`.

## Remaining production gates

Build a full-corpus PDS compact pair on an isolated channel; measure build disk/RSS,
size, conversion and replay. Exercise policy-change replay/restart and promotion,
measure unscoped tail latency against current compact on identical data, then
validate rollback/recovery. Keep the initial 10,000-row host cutoff. Do not change
production pointer, publisher pin or sole serving machine for this review.

After approval and passing qualification, admit a warmed compatible backend,
verify Worker scoped responses and aliases, then expose this UI. Preserve the
then-current Worker source. Hide/disable scoped requests explicitly before an
incompatible-backend rollback; retain compatible snapshot/overlay recovery pairs.

## Authorized rollout preparation — October 20

Nate approved the UI and authorized rollout. Candidate `b867870` passed Tangled
pipeline `3myc77ioj6522`. The scope-only disable switch passed 22 Worker runtime
checks, including disabling an already cached scoped answer while global search
remained available, then re-enabling it.

Live Worker version `6a814dee-4ffe-4b26-aa38-dc39e92f3c6c` was downloaded for source
comparison. Its `statsPage` function exactly matches the primary checkout's
uncommitted stats implementation, not repository main. That source and its tests
are carried into this release to preserve deployed behavior; the primary checkout
is untouched. This is deployment reconciliation, not a new stats redesign.

Full-corpus export/build runs in the isolated HeavyPad directory
`/home/stoat/typeahead-pds-20261020`, user units
`typeahead-pds-compile-20261020` and `typeahead-pds-full-build-20261020`.
The build uses `INDEX_CHANNEL=local` and cannot publish a production pointer.
Qualification and deployment are still pending.

## Full-corpus qualification in progress — October 20, 08:46 UTC

Reconciled Worker commit `ca50ab5` passed pipeline `3myc7m7aofj22`; its 92 Bun
tests passed. AST comparison against the live bundle now differs only in the
intended search, homepage, docs, llms and routing functions, plus the new hostname
normalizer. The deployed stats function is identical.

The read-only Turso export contains 12,650,356 rows at watermark `1792484192`.
The PDS build indexed 12,641,735 actors and 145,935,146 keys in 767.8 seconds;
the closed SQLite twin is 16,099,401,728 bytes. Compact conversion completed:
`b1792484789-3d65-px05`, 77,824-byte metadata database plus 7,203,934,208-byte
prefix map. Host eligibility includes `selfhosted.social` (773 rows) and
`pds.zat.dev` (19 rows, 18 searchable actors). There are 2,223 eligible hosts
covering 166,019 rows; 80 known hosts exceed the 10,000-row cutoff.

Current-main baseline construction uses the exact same retained export. Separate
PDS-only compact references for both hosts are built. Portable service/evaluation
binaries target `x86_64-linux-gnu.2.35`, ReleaseFast, for Fly CPU compatibility.
Full-corpus correctness, paired warm/cold latency, real read-only replay with
failure/restart/policy changes, isolated R2 publication/readback and recovery
remain pending. No production pointer or serving machine has changed.

Fresh Logfire baseline: 41 sampled compact search spans in the 30-minute window,
latest at 08:45:24 UTC, engine-span p50 1.782 ms / p95 5.798 ms. These are sampled
engine durations, not edge latency or evidence of deployed PDS support.

## Full-corpus qualification complete — October 20

The clean final run passed 5,055 byte comparisons: 895 global queries against
the current production implementation on identical data, and 790 scoped queries
against independently built PDS-only references, each at three limits. The 60
paired warm rounds covered 2,400 global queries per variant. Candidate versus
baseline p50/p95/p99 was 0.181/0.802/0.891 ms versus 0.179/0.818/0.890 ms.
Paired round bootstrap intervals show no statistically supported regression.
These are isolated engine measurements, not deployed edge latency.

Twenty paired cold-process rounds also showed no statistically supported
regression; wide confidence intervals do not establish cold-tail equivalence.
The test advises file-page eviction, then performs native admission and a readiness
query before varied queries; it is not a guarantee of fully cold OS caches.
Compact storage increased from 7,036,301,312 to 7,204,012,032 bytes (+2.38%).
Builder peak RSS was 464,384 KiB versus baseline 464,668 KiB.

Both rollback directions passed 5,175 byte comparisons following 60 real
create/migrate/hide/rename mutations, with clean shutdown and SQLite quick_check.
Seven real read-only Turso replay phases passed: baseline, failed replay, retry,
restart, changed eligibility policy, restored policy and final restart. A failed
replay did not advance readiness; retry and policy replay published the expected
policy hashes. Both final verification systemd units exited successfully.

The evaluation harness needed the production threaded I/O initialization for
remote replay and a longer startup deadline for full-corpus native admission.
An earlier report serialization failed on Python 3.10; the final clean rerun
supersedes that recovered report. These fixes did not change the qualified
production service binary.

Evidence: [correctness and warm](pds-full-corpus-20261020/correctness-warm.json),
[cold](pds-full-corpus-20261020/cold.json),
[rollback](pds-full-corpus-20261020/rollback.json),
[replay](pds-full-corpus-20261020/replay.json).

An isolated Fly machine `8d4916be311008` has no public services and a new 60 GB
volume. It runs the qualified portable binary (SHA-256
`11431ef01fed077d36d3b7ca6409a78100ac28c3a6599766414ab96fad8fedd8`).
Its R2 pair publication/readback and empty-volume native admission remain pending.
Production routing and the root publisher pointer have not changed.

## Protected publication — October 20, 11:43 UTC

The complete SQLite/compact pair passed independent R2 checksum readback before
publishing the isolated pointer. See [publication result](pds-full-corpus-20261020/publication.json).
The native generation directory was empty before the service's first successful
pointer poll; [directory evidence](pds-full-corpus-20261020/native-before-restore.json)
records that no files were pre-seeded into the restore path. Native restore,
admission and catch-up are still pending; production remains unchanged.

The abandoned 08:47 multipart upload for this task's SQLite twin was removed
only after the completed recovery object passed readback. A dry run selected
only that upload, the completed object's metadata stayed identical, and a fresh
listing found no pending uploads in that build prefix. See
[cleanup result](pds-full-corpus-20261020/abandoned-upload-cleanup.json).

Main keeps `PDS_UI_ENABLED=0` until live API acceptance so another Worker deploy
cannot expose the filter prematurely. Source commit `712f4b9` passed pipeline
`3myciu3p7ve22`. The immutable builder tag remains
`pds-compact-31ec142-20261020`; it contains the identical qualified native source.

### Native admission and transition — October 20, 14:50 UTC

The private candidate restored the protected pair into its initially empty root,
admitted `b1792484789-3d65-px05`, and caught up to 26 seconds behind. Its real
scoped, normalized, invalid, unknown and excluded-host requests passed all 24
[response checks](pds-full-corpus-20261020/native-acceptance.json). The native
ranking suite passed; the quick direct search benchmark completed without stress.
[Logfire confirmed search spans](pds-full-corpus-20261020/native-logfire.json)
from the exact qualified image. This is native acceptance, not public API release.

Worker `73c101ec-5c7f-4237-8f19-284dcab6d51e` temporarily routes to healthy legacy,
cloning the previously current Worker source. Its full authenticated smoke passed.
Old compact machine `781e732c264678` is pinned to the protected old pointer with
public services removed; its image, snapshot and live overlay remain intact.
Publisher registration and public PDS API/UI acceptance remain outstanding.

### Publisher and root transition — October 20, 15:03 UTC

The publisher's main CI was blocked by 29 existing type errors in the Agentic
publication flow, then by the Linux runner lacking `shasum`. Commits `8aa7323`
and `776c8e1` correct the typed boundaries and select the available SHA-256 tool.
Pipeline `3mycvdzctvr22` passed all checks and registration at 15:03:14 UTC:
767 tests passed, four skipped, plus type/lint/frontend checks. Its temporary
source worktree was reconciled onto main; no Cloud draft-PR publication ran.

[Registration readback](pds-full-corpus-20261020/publisher-registered.json) confirms
the immutable PDS builder tag and explicit 10,000-actor cutoff.
[Concurrency and schedule](pds-full-corpus-20261020/publisher-concurrency.json)
remain one run, `CANCEL_NEW`, and `0 9 */3 * *`.
The [queued October 22 09:00 run](pds-full-corpus-20261020/next-scheduled.json)
already carries the new parameters; it has not run yet.

[Root publication](pds-full-corpus-20261020/root-promoted.json) passed guarded
readback after the old machine was pinned and healthy. Candidate restart with
root `latest.json` and the saved public service config is in progress; the Worker
still serves healthy legacy. The
[5.45 GB compressed transport objects](pds-full-corpus-20261020/transport-cleanup.json)
were removed only after full protected-pair readback and native empty-root restore.
Protected pairs, staging manifest, rollback volumes, images and old roots remain.

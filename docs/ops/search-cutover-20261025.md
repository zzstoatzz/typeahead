# Isolated search candidate and held cutover

Preparation is in progress on October 25, 2026. This document is a plan, not a
record of a production cutover. Nate's explicit approval is required before any
production routing or transport deployment. Do not publish the shared R2
`latest.json` as part of this operation.

## Isolation and current resources

| Role | App / machine | Volume | Snapshot pointer |
| --- | --- | --- | --- |
| Serving production and immediate rollback | `typeahead-search-compact` / `86e502fe55e4e8` | `vol_vxmqwnpnlewdgzj4` | Existing root `latest.json`; untouched during this preparation |
| Candidate | `typeahead-startup-20261024` / `7841e35f965528` | `vol_v8eyllqdyykq0z5v` | `candidates/startup-20261025/latest.json` |

The candidate uses immutable image
`registry.fly.io/typeahead-startup-20261024@sha256:61b252cbb7f6bef9ec00579083b464622928da91789c3531a13fda96a365e837`,
source `f7dfca7cb6e94a3a86557003fe19de9c47a3e17e`. Its serving configuration is
two shared CPUs, 1 GiB memory, and its existing 60 GiB volume. Offline installation
temporarily uses eight shared CPUs and 2 GiB on this candidate only. The existing
image contains the reference, admission, and evaluation tools; no rebuild is needed.
Fly documents the same 32 MiB/s volume ceiling for four and eight shared CPUs;
future offline preparation should use four instead of eight when disk bandwidth
is the reason for temporary capacity. Qualification still runs at two CPUs / 1 GiB.

The full fresh snapshot is `b1792896197-ebe5-px05`: 12,757,757 actors,
147,835,341 prefix keys, source watermark 1792894100, and 7,290,933,248 prefix
bytes. Installation verifies the full artifact digests and structure before
writing the durable block-verification record. Cached startup must require that
record and verify accessed blocks. Do not use the earlier experimental bypass.

The candidate's private channel is frozen. Do not re-enable bulk publication
or point it at the shared channel during this rollout. A watchdog restarted the
candidate during automatic twin installation and discarded its partial download;
this path is not qualified for serving-time bulk promotion. Fresh generations
must be prepared offline on a separate backend until that behavior is corrected.

The fresh-snapshot restart qualification exposed a separate shutdown bug: the
recovery compactor ignored cancellation while waiting for the initial replica
sync. Canceling sync during SIGTERM left that wait active indefinitely. The local
fix exits canceled readiness waits and inter-chunk waits and records incomplete
compaction rather than completion. Its real SQLite / threaded-I/O regression
fails before the fix and passes after it; native validation passes 148 tests with
four skips. Linux and candidate lifecycle qualification of this update remain
required. Build a runnable Linux index test artifact with `zig build
test-index-binary -Dtarget=x86_64-linux-gnu -Doptimize=ReleaseSafe` and run that
artifact on the candidate; its temporary fixture does not touch the live overlay.

Remote candidate artifacts are being preserved beneath
`releases/startup-candidate-20261025/`, outside the expiring `staging/` and
`builds/` prefixes. Require the protection receipt and full readback checksums
before cutover. The same-watermark legacy twin remains a recovery artifact in
R2; candidate process recovery uses its locally admitted compact generation.
Immediate traffic rollback uses the intact production backend and its live overlay.

## Required evidence before approval

- Candidate private pointer and actual machine configuration agree; production
  machine, volume, shared pointer and routing have not changed.
- Installation and artifact protection receipts pass, including full SHA-256
  readback. Candidate local file identity matches its admission record.
- Frozen reference and candidate responses match for the complete existing
  global/PDS matrix on the fresh snapshot and the same overlay. Include cold,
  warm and concurrent requests at two CPUs / 1 GiB.
- Three cached process starts become ready in under one second. Removing the
  admission record fails closed. Separately measure a clean VM restart, including
  process launch, overlay opening, and attachment; report any crash-recovery caveat.
  Use `fly machine restart 7841e35f965528 -a typeahead-startup-20261024
  --signal SIGTERM --time 60 --skip-health-checks` for that isolated restart.
  The service registers SIGTERM for graceful draining; this CLI defaults to
  SIGINT. Verify `shutdown complete` in the logs rather than calling an
  interrupted SQLite recovery a clean-start measurement.
- Candidate `/ready`, `/health`, `/index/status`, exact handle, global search,
  eligible PDS and excluded PDS behavior pass. Sync lag stays below 600 seconds;
  snapshot age stays below four days. Freshness thresholds must remain unchanged.
- Run repository ranking and engine benchmarks against the explicit candidate
  origin. Report structural failures and existing Bluesky parity gaps separately;
  don't describe a benchmark with failing criteria as passed.
- Transport allowlist build dry-run passes. The existing transport deployment
  and public Worker versions are captured, with a drift check immediately before
  applying. This preparation has not deployed either Worker.

## Approved cutover procedure

Execute these production mutations only after explicit approval and passing the
gates above. Recheck candidate readiness and the rollback backend immediately
before starting. Abort on unexpected version drift.

Read-only observations on October 25:

- Public Worker `typeahead`: 100% version
  `e331b7b4-cbca-4a28-9270-29af2fc2b7bb`.
- Transport `typeahead-search-transport`: 100% version
  `786da370-0cc2-499b-9238-25e25de81267`.
- `SEARCH_BACKEND_URL` is an existing `secret_text` binding. Preserve the
  deployed public Worker code and other bindings; do not deploy the whole
  public Worker from this worktree.

1. Capture fresh `wrangler deployments list --json` for both named Workers and
   require the expected current versions. Preserve a pre-cutover receipt.
2. Deploy only `wrangler.search-transport.jsonc` from the reviewed revision. Its
   only intended source change adds the candidate's exact HTTPS origin to the
   existing GET `/search` allowlist. Keep the two existing origins allowed.
3. Change only the public Worker's `SEARCH_BACKEND_URL` via `wrangler secret put
   SEARCH_BACKEND_URL --name typeahead`, passing
   `https://typeahead-startup-20261024.fly.dev` on stdin. This immediately creates
   and deploys a version based on existing deployed code; it is the traffic switch.
4. Verify both public lexicon aliases, exact-handle rank, global and PDS-scoped
   queries, PDS acknowledgment, `/health/freshness` HTTP 200 with the fresh build,
   backend lag, and latency. Preserve timestamps, HTTP results and version IDs.
5. If checks fail, immediately restore the old URL using the same binding-only
   operation. Do not change snapshot pointers or resize/restart production to
   repair a failed candidate.

Wrangler command semantics were checked against installed 4.152.0 and the
[official command reference](https://developers.cloudflare.com/workers/wrangler/commands/workers/).
`secret put` deploys immediately; do not run it merely to prepare the plan.

## Rollback and cleanup

Traffic rollback sets `SEARCH_BACKEND_URL` to
`https://typeahead-search-compact.fly.dev` through the same binding-only command.
Verify the old build, exact-handle and PDS queries, and availability after rollback.
The old snapshot is stale, so rollback restores availability but does not resolve
the freshness alert. Do not hide that distinction or mute the alert.

Retain production's current machine and volume through acceptance. The expanded
transport allowlist can safely retain both origins while traffic rolls back.
If the transport deployment itself fails, restore its captured pre-cutover
version using `wrangler rollback --config wrangler.search-transport.jsonc
786da370-0cc2-499b-9238-25e25de81267 --yes`; first check that no unrelated update
has occurred. Do not roll back the entire public Worker to an old source version
when a binding-only restoration is sufficient.

Temporary resources to reconcile after approval/acceptance:

- Candidate machine and volume become serving resources if accepted; otherwise
  destroy this specific machine and volume after reverting traffic.
- Restore candidate preparation capacity to two CPUs / 1 GiB before qualification.
- Keep compact snapshot, admission sidecar, live overlay, private channel and
  protected remote artifacts when the candidate becomes production.
- Keep the original serving backend for the agreed rollback period. Ask Nate
  before retiring this rollback path; no retention period has been agreed yet.
- Preserve qualification JSON and logs in the repository, then remove obsolete
  test fixtures, temporary transfer files and rejected offline generations.
- Record and remove the candidate-specific cloud protection helper and its
  completed transient service after retaining its receipt. Do not remove shared
  publisher resources or unrelated archive generations.

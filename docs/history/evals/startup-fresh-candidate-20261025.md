# Fresh full-corpus candidate qualification

October 25, 2026. During qualification production remained on its original
machine, volume, image, shared snapshot pointer, transport and routing. Nate
subsequently approved cutover; the [execution record](../ops/search-cutover-result-20261025.md)
shows public and Evergreen checks passing on this fresh backend.

## Candidate

The final image is
`registry.fly.io/typeahead-startup-20261024@sha256:9822d2b735cbfd5cda65b1bc4b8c6f95fcfd74e3d109c78e22b9a5b596247573`,
source `b39e21d9b5f67832f1f206870e23e531a7c5a192`. It runs on
`typeahead-startup-20261024` machine `7841e35f965528`, volume
`vol_v8eyllqdyykq0z5v`, two shared CPUs and 1 GiB RAM. The private channel is
`candidates/startup-20261025/latest.json`; it does not follow the shared root.

Snapshot `b1792896197-ebe5-px05` contains 12,757,757 actors and 147,835,341
prefix keys. Its complete 7,290,933,248-byte prefix file is installed and verified.
The admission sidecar binds its exact opened file identity and expected digest.
Startup uses that receipt and verifies blocks as read; it does not omit validation.

## Full data measurements

On the fresh snapshot and the same frozen pre-compaction overlay, the original
full-scan reader required 432.835 seconds. The admitted reader became ready in
18.96, 15.47 and 17.38 ms. All 78 cold, 78 warm and 24 concurrent responses matched
the reference. Cold median was 72.63 ms, maximum 1,259.69 ms; warm median was
14.49 ms, maximum 22.42 ms. This phase used the prior admitted image.

Offline overlay preparation then preserved exactly the rows the native compactor
retains, including nonzero-source overrides. Each table's ordered logical hash
matched its source selection, the cursor and schema were preserved, and SQLite
integrity verification passed. The resulting overlay is 81,043,456 bytes.

On that same prepared overlay, the final image matched the previous admitted
reader for all 79 cold, 79 warm and 24 concurrent requests, including an actual
excluded PDS selected from the snapshot. Three process starts took 23.77, 12.28
and 17.07 ms. Cold median was 30.52 ms, maximum 1,523 ms; warm median was 14.77 ms,
maximum 17.99 ms. The concurrent maximum was 133.46 ms. An additional cold sanity
query on a later start took 1,587.46 ms. Sub-second readiness does not imply every
disk-cold query is sub-second. Removing the admission sidecar fails closed.

Both Mac and real Linux index suites pass 148 tests with four skips. The Linux
tests ran inside the final candidate image with an empty credential environment,
using their own temporary SQLite fixtures. Artifact digests were compared with
the locally built executables before running them.

## Shutdown, live serving, and transport

Restart qualification exposed a real bug: recovery compaction swallowed cancellation
while waiting for initial sync. SIGTERM canceled sync but could leave that wait
active; the previous image failed to stop within 60 seconds. The final image exits
canceled waits and records compaction as incomplete. Its regression test fails
before the fix and passes after it using real SQLite and threaded I/O.

The final clean SIGTERM restart logs `shutdown complete` and reattaches the index
71.324 ms after process launch. Fly VM boot adds platform time; this number is
process startup, not total VM restart time. Post-restart HTTPS checks pass for
readiness, health, fresh build identity, exact-handle rank, global search, hostname
and URL PDS forms, excluded-PDS rejection and empty unknown-PDS results.

The real freshness handler, executed locally against the candidate's HTTPS origin,
returns HTTP 200, `stale=false`, probe success and zero replica lag. This is a
candidate check; the public production freshness endpoint still reports the old
snapshot. The prepared transport source fetches global and scoped requests
successfully, rejects an unlisted origin, and passes the Wrangler deployment
dry-run. Neither Worker was deployed.

The canonical ranking benchmark passes every acceptance criterion. The quick
engine benchmark also exits successfully: median across query cold p50s is 76.85
ms, and median across warm p50s is 53.2 ms over external HTTPS. These network
measurements and the loopback disk-cold matrix above measure different paths.

## Readiness and held cutover

Runtime verification, full SHA-256 readback of the protected artifacts, private
manifest acceptance and final isolation/drift checks pass. The candidate is ready
for the subsequently approved traffic cutover. Production was unchanged throughout
preparation.
The native watcher fetched the corrected manifest on the clean restart; its
downloaded SHA-256 matches the protected pointer, and no promotion error followed.
Both deployed Worker versions match their recorded pre-preparation versions.

The channel remains frozen and the bulk publisher schedule remains inactive.
Future snapshot installs must be prepared offline; the serving-time bulk install
path is not qualified. Before the fresh snapshot exceeds the four-day threshold,
another offline refresh is required. Do not mask that constraint by changing
freshness thresholds or re-enabling unsafe publication.

The [cutover and rollback plan](../ops/search-cutover-20261025.md) preserves the
current production backend throughout acceptance and inventories cleanup.

## Evidence

- [Full installation](startup-fresh-installation-20261025.json)
- [Original-reader comparison](startup-fresh-frozen-20261025.json)
- [Offline overlay verification](startup-offline-overlay-20261025.json)
- [Final-image comparison](startup-shutdown-fixed-20261025.json)
- [Image and executable identity](startup-shutdown-image-20261025.json)
- [Clean restart and live HTTPS](startup-fresh-live-20261025.json)
- [Real freshness and transport-source checks](startup-fresh-edge-source-20261025.json)
- [Ranking and engine verdicts](startup-fresh-benchmarks-20261025.json)
- [Engine measurements](startup-fresh-engine-20261025.json)
- [Protected artifact readback](startup-protected-artifacts-20261025.json)
- [Private manifest acceptance](startup-private-manifest-20261025.json)
- [Isolation receipt](../ops/search-isolation-20261025.json)

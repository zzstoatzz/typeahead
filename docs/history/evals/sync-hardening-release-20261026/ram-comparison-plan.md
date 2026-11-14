# Inactive RAM comparison plan

Owner: this Typeahead release chat. Target: managed inactive Machine
`080d415b734708` on `typeahead-startup-20261024`, existing volume
`vol_4915xomx28k0ml5r`, scratch/data root `/data/refresh`.
Disk budget: the existing 20 GB volume; no corpus copies or new disks.
Production Machine `85379da4636338` and routing remain unchanged.

Start only after one clean replay and matching persisted PDS policy. Preserve
image `b0492f07…`, snapshot, overlay, and routing ETag throughout. Compare two
shared CPUs at 1 GiB, 2 GiB, then 1 GiB again, using cached restarts of the same
Machine and volume. Record exact configs, instances, timings and response hashes.

An attempted temporary Machine URL override was rejected by the runtime isolation
check before measurement because the app secret took precedence. Configuration
was restored. The successful comparison retains normal connectivity, waits for
startup sync to complete and for its watermark to stay quiet for five seconds,
then runs a <=120-second workload inside the subsequent five-minute sync interval.
Watermark/compaction markers and per-query response hashes detect interference or
data drift. Each remote client has termination handlers and a hard deadline.

Use the same deterministic bounded prefix workload in every arm, with repeated
rounds to distinguish first-touch and warm behavior. Compare response hashes for
identical queries; report concurrent-work/cache/host limitations rather than
claiming the memory setting alone proves causation. The final controller still
specifies two CPUs / 1 GiB unless Nate approves a different production size and
that change is landed and pinned first.

Cleanup: stop each temporary client; restore the original configuration and
connectivity on success, failure or cancellation, confirm instance state and
policy/freshness, remove protected temporary config backups, and retain the
small script, query set, hashes and result receipts here. The candidate remains
managed production preparation, not an abandoned trial server.

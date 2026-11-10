# Shadow slot bootstrap

How an empty shadow-slot volume gets its first index and store. The script beside this document seeds the index today; the store steps are placeholders that stop with an error. Nothing here has run on a Fly machine.

Everything happens on the machine. The bundle goes from R2 to the volume and never through a laptop or a home network.

## Sequence

1. Create the app, a 40 GB volume and the secrets, and start the machine. With no receipt on the volume the supervisor starts neither process and waits.
2. Open a shell on the machine and run the bootstrap script, optionally naming a build. It then:
   1. picks the newest build under `releases/search-tantivy/` that has both a manifest and a bundle;
   2. reads the manifest and checks its identity, size and checksum fields;
   3. checks free disk, downloads the bundle, and verifies its size and SHA-256 against the manifest;
   4. extracts only the index files, verifies each against the manifest's inventory, deletes the bundle, and asks the reader binary to open the result;
   5. loads the store from the actor documents stored in that index (placeholder);
   6. seeds the relay, labeler and PLC cursors (placeholder);
   7. moves the index into place, writes the receipt, and removes the scratch directory.
3. Restart the machine. The supervisor finds the receipt and starts both processes.

With `--index-only` the script skips steps 5 and 6 and records `store_loaded: false`. That is enough to serve the bundle's index from the shadow, and nothing more.

The store load in step 5 is what the [zkv measurement](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/evals/zkv-scale-20261110/README.md) did: its actor extraction tool prints every stored actor from the index as one JSON line, and its loader commits them with backpressure against compaction. That run took 17.5 minutes for the load and 90 seconds to settle on a 4-vCPU machine with local NVMe. Both tools are experiment code and neither is in the image.

## R2 credentials

The script needs the four `INDEX_R2_*` values and only reads: one listing of the release prefix and two object downloads. Cloudflare's [R2 token documentation](https://developers.cloudflare.com/r2/api/tokens/) lists an "Object Read only" permission that can be scoped to chosen buckets, so a read-only token for the `typeahead-index` bucket can be created. Whether one already exists is not recorded in this repository or in the operations repository; the index deployment's `index-r2-*` secret blocks publish to the bucket and so can write. Create a new read-only token for the shadow. The same bucket holds the routing record, which is the reason not to reuse a writing token. The documentation does not describe scoping a token to a key prefix, so the shadow's token can read the control objects as well as the releases.

The credentials can be unset once the receipt is written.

## Disk during bootstrap

| step | on disk | GB |
|---|---|---|
| bundle downloaded, index extracted | bundle 6.4 + index 4.9 | 11.3 |
| store loading | index 4.9 + load peak 10.0 | 14.9 |
| store settling | index 4.9 + compaction peak 18.6 | 23.5 |

The bundle also carries a 1.5 GB PDS fallback database for the Turso poller; the script does not extract it. The peak is the settle at 23.5 GB, inside the 40 GB volume. The script requires bundle plus index plus 1 GiB free before it downloads.

## Cursors

The manifest carries `source_watermark`, a Unix time in seconds: the Turso clock before the checkpoint minus the checkpoint's replay overlap. It carries no relay sequence. A relay cursor is a sequence number that only means something to the relay that issued it, and production's lives in Workers KV under `relay_cursor`, readable through the Worker's admin endpoint with the admin secret, which the shadow must not hold. That value is also the present position, newer than any bundle.

So "older than the bundle" has to be turned from a time into a sequence. The options:

- **Probe the relay.** Every frame carries its sequence and its time. Connect at a candidate cursor, read the first frame's time, and step back until it is older than the watermark minus a margin. A cursor of zero asks for the oldest retained event, which bounds the search. This needs no secret and proves the property it relies on. It is the recommended path and belongs in the store process.
- **Start from an operator's hint.** The store process takes an optional bootstrap relay cursor. An operator reads production's current cursor off the machine and passes a lower number. The store process still has to confirm it against the first frame's time.
- **Record the relay cursor in the manifest** when the checkpoint is exported. This is the cleanest input but changes the publisher in the operations repository, for a bundle path the design intends to retire.
- **Ask the Stream archive**, which keeps sequence and time for every event back to sequence 1. It needs an archive key on the machine.

The receipt records `replay_before`, the watermark minus a margin (an hour by default), as the time the first replayed event must not be newer than.

## Before the store process may advance a cursor

All of these must hold, and the store process has to check them itself:

1. The load is complete: the number of actors committed equals the number the index reported, and the load's completion is recorded in the store in the same commit as its last batch.
2. The first frame received at the seeded relay cursor is from the pinned relay host, has a time at or before `replay_before`, and was not preceded by an `OutdatedCursor` notice. If the relay no longer retains events that old, the bundle is too old to bootstrap from and a newer one is needed.
3. The labeler and PLC cursors are seeded the same way: PLC by time, which the watermark gives directly, and the labeler by sequence, which needs the same kind of proof.
4. The index bridge's position in the change log is initialised, so that every change committed after the load reaches the index. The loaded actors themselves are already in the index.
5. From then on a cursor only moves in the same commit as the data it covers.

Replay is meant to be idempotent, but a replayed event is older than the state the bundle already holds for that actor. The store ends up correct only after the replay has passed the watermark, so the comparison job must not start before the committed relay position's event time is newer than the watermark.

## Open questions

- How long `bsky.network` retains events. The newest bundle is up to about half a day old plus its build time; the margin and the retention window have to cover that. Not verifiable from local files.
- The margin. An hour assumes events reached Turso within an hour of the relay emitting them. During the 2026-11-09 incident the ingester ran well behind; a bundle built in such a period needs a wider margin or the lag recorded with it.
- Fields that do not come from the firehose (identity resolution, profile enrichment, labels). Whether replaying an older commit can overwrite them, and what restores them, depends on the store process's write rules.
- Where the labeler cursor's starting point comes from, and how far back the labeler retains.
- Whether the shadow's reader build opens a bundle written by the production reader under every format the publisher may emit; the script's status call is the check.
- Load time and stall behaviour on a Fly volume with shared CPUs.

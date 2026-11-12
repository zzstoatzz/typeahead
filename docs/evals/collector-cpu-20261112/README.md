# Collector CPU, 2026-11-12

Where the store process ("collector") spends its CPU, measured on the serving slot and in a local benchmark, and what the `event-cpu` branch changes. Nothing here is deployed: the branch is local, and every production figure was read from `typeahead-shadow-a` without changing it.

## Result

The cost was not mainly per relay event. On the slot, 71% of the process's CPU was the owner thread re-reading dead queue keys on every lease, and most of the rest was the relay reader decrypting TLS in software.

| change | commit | what it removes | measured |
|---|---|---|---|
| queue floors | `2769b63` | leases, prunes and health counts scanning a timed range from time 0 | refresh step 11.7–19.7 ms → 2.4–2.6 ms CPU, no longer growing with uptime |
| known-actor cache | `f6333b6` | one store read per sighting of a known actor | owner stage 28.9 → 10.9 CPU-ms per 1,000 events (1.6M events, same seed, back to back) |
| AES build of the store binary | `b6aace0` | software AES-GCM on the relay's TLS stream | TLS stage 43.1 → 2.3 CPU-ms per 1,000 events between a build without and with AES instructions |
| short decode, **option, not adopted** | last commit on the branch | SHA-256 over every commit's blocks | decode stage 11.0 → 1.8 CPU-ms per 1,000 events in the software build, 2.9 → 1.7 with SHA instructions |

The store digest is identical before and after every change, for the same seed.

## Production, read without touching it

Raw readings: [production-threads.txt](production-threads.txt). Per-thread CPU from `/proc/<pid>/task/*/stat`, I/O from `…/io`, counters from `/health`, twice, 60 s apart, at 241 relay events a second.

| thread | CPU in 60 s | share | what it was doing |
|---|---|---|---|
| owner (store writer) | 10.66 s | 71% | 5,285 `pread`s a second, 20.7 MB a second read, for 213 changes a second |
| relay reader | 3.23 s | 22% | 223 µs per relay event |
| everything else | 1.03 s | 7% | identity and enrichment workers, bridge, corpus scan, HTTP |
| process | 14.92 s | 0.249 of a CPU | |

Two things follow from those numbers alone.

The owner's reads do not scale with events. 213 changes a second at two block reads each is about 500 reads a second; the thread made ten times that, and since boot the rate had risen from an average of 3,170 a second to 5,285. Total bytes read since boot were 450 GB against 16 GB written.

The "0.44 ms per relay event" figure this work started from divides all of the process's CPU by relay events. The part that is per event is the reader's 223 µs and a small share of the owner's.

## Benchmark

`services/src/collector_bench.zig`, one build step:

```sh
cd services
zig build bench-collector -Doptimize=ReleaseSafe                         # this machine's CPU
zig build bench-collector -Doptimize=ReleaseSafe -Dcpu=native-aes-sha2  # software AES and SHA-256, as the image's baseline build has
zig build bench-collector -Doptimize=ReleaseSafe -- --events 1600000 --history 0 --walk 0 --sweep 0
```

It builds a store of 1,000,000 synthetic actors once (12 CPU-seconds, 400 MB, under `services/.zig-cache/collector-bench`, or `--dir`), copies it, and drives 400,000 `subscribeRepos` frames through the path the role uses: one AES-256-GCM record decryption per frame, zat's frame decode, `Intake`, `events.Changes`, and the `Owner` committing to the store. Frames are built by zat's own encoders: a `#commit` carries a CAR with a commit block, three tree blocks and the record, 2,336 bytes on average. The owner is stepped on the same thread after every batch, under a clock that moves 1/480 s per event, so two runs of one seed do the same work. Every two simulated seconds it also runs the enrichment loop's store work (walks, a lease, the results), after `--history` such steps before the measured stretch.

Shares are flags, in events per thousand. The defaults come from the slot's counters over 14.19M events:

| flag | default | from |
|---|---|---|
| `--unwanted` | 130 | changes applied were 86.4% of events |
| `--deletes` | 40 | assumption |
| `--profiles` | 7 | 98,000 refreshes not queued by a walk |
| `--unknown` | 1 | 12,588 actors created (0.09%), not the 1% first assumed |
| `--identity`, `--account` | 1, 1 | 26,582 identity leases, 3,449 status changes |
| `--active`, `--skew` | 150,000, 2.0 | gives 77 hourly stamps per 1,000 events; the slot's flush volume implies about 80 |
| `--walk`, `--sweep` | 20,000, 10,000 an hour | the role's defaults |
| `--history` | 1,000 steps | 33 simulated minutes of leases; the slot had ten hours |

Actors start with last-activity stamps in the steady state the mix implies (an actor heard at rate r is stamped every 3600 + 1/r seconds), so stamps arrive at their long-run rate from the first event.

It prints CPU (`getrusage`, user + system) per 1,000 events by stage, commits, changes, bytes committed, flushed and compacted, peak RSS, and a digest. `decode` is timed in a pass of its own over the same frames, and `intake` is the remainder. `refresh` is the enrichment loop's store work, reported beside the relay path and not in its total.

The digest is SHA-256 over every key and value of each key range (actors, change log, handles, identity queue, labels, policy, quality, refresh queue, tombstones, cursors), plus one over the actor fields a change-log consumer reads and one over the request bodies `bridge.wire.encodeReconcile` produces for every actor. `digest: store=` combines the ranges: equal means the store holds the same bytes under the same keys.

### How it relates to the slot

The laptop is an Apple M5 Pro; the slot is a shared Xeon at 2.1 GHz running a ReleaseSafe build for baseline x86-64. The reader's stages cost 25 µs per event here in the software-crypto build ([first-baseline-unloaded.txt](first-baseline-unloaded.txt)) and 223 µs on the slot, about nine times more; the slot's figure also includes socket reads and WebSocket framing, which the benchmark leaves out. The benchmark is used for relative change and for the split between stages; the slot's thread readings are the weights.

Other sessions loaded this machine during most runs (load average 140 to 480). CPU time per stage rose about 2.3 times under that load: the TLS stage, which no change touches, read 0.99 unloaded and 2.2–2.4 loaded. Comparisons below are back to back, and the TLS figure in each line shows the load it ran under.

## Baseline

Relay only, unloaded, commit `a67c360`: 12.3, 12.7 and 12.9 CPU-ms per 1,000 events (TLS 1.0, decode 1.5, intake 0.1, owner 10). Without AES and SHA instructions: 36.3 (TLS 19.3, decode 5.7, intake 0.2, owner 11.1).

With the enrichment loop's steps, commit `8b25db1`, three runs under load ([baseline-native.txt](baseline-native.txt)):

| run | relay path | TLS | decode | intake | owner | refresh |
|---|---|---|---|---|---|---|
| 1 | 45.3 | 2.36 | 2.91 | 0.58 | 39.4 | 16.3 |
| 2 | 34.4 | 2.22 | 2.80 | 0.55 | 28.8 | 14.8 |
| 3 | 35.0 | 2.22 | 2.81 | 0.59 | 29.4 | 15.3 |

A refresh step cost 1.0–1.7 ms at the start of the history and 11.7–19.7 ms during the run. With `--history 2000` it was 1.6 ms at the start and 22 ms after 33,000 leases.

Profile (`sample`, native build, relay path and refresh):

- `Refresher.step → Owner.turn → Batch.apply → read.timed → merge.Merge.nextCandidate → loadBlock`: the lease scanning its range. A quarter of all samples at `--history 2000`.
- `Batch.apply → read.actor → Db.getOnce`: the point read per sighting. `fetchFences → DirStore.reader → openat` is the largest part of it on macOS, because zkv's directory store keeps eight open files and the store has 51 tables (615 on the slot); then two `preadv` per read; then the block cache's hash map.
- `car.verifyBlockHash → Sha256`: most of the decode.
- `Batch.commit` 3.6% and `Maintainer.step` 3% of samples after the first change.

## Changes

### Queue floors

An entry leaves the identity queue, the refresh queue or the tombstone range by deletion, and zkv keeps a deleted key until a compaction reaches it. `identity_lease`, `refresh_lease`, `tombstones_pruned`, the identity backlog count and the health snapshot's counts all scanned their range from time 0, so each read every key the range had held since the last full compaction. The enrichment loop leases every two seconds. This is the same fault as the change-log trim fixed in `6e03564`, in three more ranges.

`Store` now keeps, per range, a time below which there is no entry (`Floors`). A lease or prune starts its scan there and moves the floor up to the first entry it finds, or past its bound when it finds none. `save`, the only place a timed key is put, moves it down when it puts an earlier one. A batch works on a copy that replaces the store's on commit and is dropped on abort. It is not stored: after an open the first scan of each range walks it once.

Before → after ([floors-native.txt](floors-native.txt)): refresh step 11.7–19.7 ms → 2.4–2.6 ms, flat through the run; refresh work per 1,000 relay events 14.8–16.3 → 2.9 CPU-ms; the benchmark's wall time 221 s → 34 s. Digest `e43aa5d8789eab17` before and after.

Tests: a key planted below the floor for an actor that does not exist is not found by a lease (a scan from 0 would fail on it); an entry queued for a time before the floor is still leased, including under a clock that stepped back; a lease in an aborted batch does not hide what it leased; a reopen starts over.

### Known-actor cache

A sighting of a known actor writes only when the hourly stamp is due or `hidden` would come out different, and it read the record to learn that neither was. After the floors that read was 55% of all samples.

`Store.seen` maps a did:plc to the last second for which a sighting of it writes nothing. `save` is the only place an actor record is written, and on every write it sets the entry (a live actor with no expiring label: `last_activity_at + 3600`) or removes it (not live, or a label with an expiry, whose lapse changes `hidden` with time alone). So creation, deletion, deactivation, reactivation and relabelling each update the entry in the write that makes them. The whole map is dropped when a batch aborts, when a domain is blocked or unblocked, and at `seen_limit` (262,144 entries of 40 bytes; `0` turns it off). It is empty after an open. It is a map of exact keys, not a filter: a DID that is absent is read, as before, so there is no false "known".

This is the answer to the old ingester's bloom filter, which was never reset: nothing here outlives the write it describes.

Before → after: owner stage 34.1 → 21.6 CPU-ms per 1,000 events over 400,000 events, where 30% of events are an actor's first sighting of the run and still read ([seen-native.txt](seen-native.txt)); 28.9 → 10.9 over 1,600,000 ([long-run.txt](long-run.txt)), relay path 34.7 → 16.8. Peak RSS 63 → 77 MB. Digests equal in both pairs.

Tests: a record removed by hand is not recreated by a sighting inside the hour (a read would recreate it), and the stamp still lands at 3601 s; a blocked domain, a lapsed label, a departed and a reactivated actor are each seen by the next sighting; a sighting in an aborted batch is not remembered; the cap and a reopen keep the outcome.

### AES build of the store binary

The image builds for baseline x86-64 (`-Dtarget=x86_64-linux-gnu`). `std.crypto` chooses AES and GHASH implementations at compile time, and uses the CPU's only when the target has `aes`, `avx` and `pclmul`. A client built without them offers `TLS_AES_256_GCM_SHA384` last, and bsky.network picks it anyway. In the benchmark built the same way, TLS is 43.1 of 76.8 CPU-ms per 1,000 events ([seen-soft-crypto.txt](seen-soft-crypto.txt)) against 2.3 with the instructions.

A binary built for instructions a CPU lacks dies with SIGILL. Fly does not publish a guaranteed feature set for its hosts; the two under the slots report all three (a Xeon and an EPYC, read 2026-11-12), which is two hosts. So the image holds both binaries, and `supervise` runs the `x86_64_v2+aes+avx+pclmul` build only when `/proc/cpuinfo` lists every feature it needs, the baseline build otherwise, and logs which. That feature set is the least that switches `std.crypto` over; it does not need AVX2.

Verified by cross-compiling both: the `.aes` binary links `crypto.aes.aesni` and no software AES or GHASH; the baseline links `aes.soft`, `aes.bitsliced` and `clmulSoft`. Not verified: the image build itself, and the binary running on a Fly host. `just test-supervise` covers the choice with stand-in binaries and a stand-in `cpuinfo`.

SHA-256 stays in software on the Xeon either way: it has no SHA extensions, and `std` needs `sha` and `avx2` in the target to use them.

## Option: decode a commit without hashing its blocks

Not adopted. It is the last commit on the branch so the rest stands without it.

**What is verified today.** The role calls `zat.firehose.decodeFrame`. For a `#commit` that function parses the CAR in `blocks` and recomputes the SHA-256 of every block against the CID the CAR itself gives for it (`car.read`), then decodes the record of each op by looking its CID up among those blocks. That is all. Nothing in the role checks the commit's signature against the repo's signing key, proves the MST, checks that `repo` is the DID the commit block names, or calls `zat.verifyCommitCar`. If the CAR fails its hash check the event is not dropped: zat discards the CAR and returns the event with no records.

**What that check protects in this store.** It guarantees one thing: the bytes of a record are the bytes its op's CID names. Both the CID and the bytes come from the same frame, so a relay that wanted to lie supplies a matching pair; the check catches corruption, not forgery. The store reads a record in one place, a create or update in `app.bsky.actor.profile` or `sh.tangled.actor.profile`, to take a display name, description, avatar and banner. Everything else it takes from a commit comes from the frame's unhashed payload: the repo DID (creates the actor, queues identity resolution, stamps activity) and each op's collection and action. `#identity` and `#account` frames carry no blocks at all, and they queue identity work and change an actor's status. So what a malicious or broken relay can make this store do today, verification or not: create actors for any DID, stamp activity, set any profile text for any DID, deactivate, delete or reactivate any actor. Handles are the exception: they come only from identity resolution against the DID's own authority, never from the relay.

**What the option changes.** `frame.decode` makes the same CBOR calls on the header and payload as zat, so it refuses the frames zat refuses. A commit with a create or update in a profile collection goes to `zat.firehose.decodeFrame` unchanged, hash check included. Every other commit is returned without parsing its CAR. For those commits the store never read a record, and the result of the hash check never changed what it did with them, because a failed check only removed records. The set of effects reachable from an unverified frame is the same as today.

What does change: the collector would no longer hash blocks it does not use, so it would not notice corrupted block bytes in a commit it only counts as a sighting (it does not notice today either: nothing is logged or counted when `car.read` fails); the fast path restates 40 lines of zat's private `decodeCommit`, held to it by a test that runs both decoders over well-formed frames, every truncation and four mutations of every byte; and zat's raw-frame hook advances its reconnect cursor past a frame the handler could not decode, where the decoded-event hook does not, which changes which frame a reconnect resumes at and nothing the store holds. If the owner wants the relay's commits verified for real (signature and MST), that is a different change in the other direction, and this option does not make it harder: it would sit where the full decode is called.

**Measured** ([option-short-decode-native.txt](option-short-decode-native.txt), [option-short-decode-soft-crypto.txt](option-short-decode-soft-crypto.txt), against [seen-native-2.txt](seen-native-2.txt) and [seen-soft-crypto.txt](seen-soft-crypto.txt) under the same load): the decode stage goes 11.0 → 1.8 CPU-ms per 1,000 events in the software-crypto build, where the relay path goes 76.8 → 63.4, and 2.9 → 1.7 with SHA instructions, where the relay path goes 23.2 → 22.6. The figure after includes the second payload decode zat's raw-frame hook makes to read the seq. Digest `e43aa5d8789eab17`, unchanged.

A cleaner form of the same thing is an option on zat's decoder to skip CAR hydration, which would need no restated rules. zat was not modified.

## Candidates checked and left alone

- **Writes that change nothing.** A sighting that changes nothing staged no write before this work and stages none now; what it cost was the read. The hourly stamp rewrites the whole actor record, about 290 bytes, and no secondary key and no change-log entry: 77 per 1,000 events, 22 bytes committed per event, which is nearly all the relay writes. Removing or coarsening the stamp is a behaviour change and was not made. `last_activity_at` is read by `/store/actor` (`store_process/http.zig`) and by tests; the index bridge sends 0 in its place. Stamping daily would cut the relay's store writes by about 95%.
- **Commit batching and cursor writes.** 2.9 commits a second on the slot; `Batch.commit` with its fsync was 3.6% of samples. Larger batches would trade the bridge's freshness for little. No change, so no new kill test.
- **Change-log growth and trim.** Already floor-based; not visible in the profile.
- **Allocation.** The per-frame arena and the batch arenas were under 1% of samples. The option reuses one arena across frames; nothing else was worth changing.
- **zkv compaction settings.** Maintenance was 3% of samples at this write volume: 4–14 flushes and 1–3 compactions per 400,000 events. No setting was changed. On the slot a flush happens every 256 commits (about a minute), before the memtable fills.

## For zkv

zkv was not modified. Two things in it shape the remaining cost:

- `DirStore` keeps eight open files and finds one by scanning the array. With hundreds of tables and random keys nearly every point read opens and closes a file. A table of handles sized to the table count and indexed by name would remove that; `DirStoreWithHandles(N)` already takes the count, but the lookup is linear in N.
- A scan of a range where entries are deleted from the front reads every deleted key until a compaction drops them. The floors work around it for three ranges this store owns. A range delete, or a merge iterator that can seek past a run of deletions, would fix it for any caller.

## Projection

At 480 events a second on the slot, from the thread readings and the benchmark's ratios. These are estimates; the first real number is a `/proc` reading like the one above after a deploy.

| part | today | after the three changes | reasoning |
|---|---|---|---|
| owner | 0.18 of a CPU, not falling with fewer events | 0.02–0.05 | nine tenths of its reads were scans; the cache removes about nine tenths of the rest of the reads. The slot's store is 13 times the benchmark's, so its per-read cost is the uncertain term |
| relay reader | 0.11 | 0.03–0.05 | TLS was 77% of the reader's stages in the software build; socket and WebSocket work is not in the benchmark |
| other threads | 0.02 | 0.01–0.02 | their HTTPS also moves to hardware AES |
| collector | 0.30 | 0.06–0.12 | |

The index process used about 0.025 of a CPU since boot by its threads' counters. Together that is under 0.25 with margin. Under 0.125 is plausible and not shown; the option would take another 0.01–0.02 off the reader.

What would dominate afterwards: SHA-256 over commit blocks on the reader, then the stamps' writes and the compaction they cause, then what a point read costs on a store with 600 tables.

## Risks for adoption

- The floors and the cache are in-memory state that must match the store. Both are maintained only by `save` and the scans, and dropped on abort; a new code path that writes a timed key or an actor record without going through `save` would break them silently. `tombstones_pruned` deletes actor records directly, which is safe only because a tombstoned actor is never in the cache.
- The cache holds up to 10 MB at its cap. Peak RSS rose 14 MB in the benchmark.
- After a restart the first lease of each queue walks the dead keys once. On the slot today that is about 40 MB of reads, once.
- The `.aes` binary has not run on a Fly machine. The supervisor falls back by flag, not by trying it; a host that reports a flag and traps anyway would restart-loop the store while the index keeps serving. `fly logs` shows which binary was chosen.
- The new supervisor tests first stopped a slot before its stand-in index had set its signal handler, which failed about one run in five under load. They now wait for the index as the older tests do, and four runs in a row passed.

## Verification

At the head of the branch without the option, and again with it: `cd services && zig build test` (20 of 20 steps), `just --no-dotenv test-bridge`, `just --no-dotenv check`. The differential and model tests run with the floors and the cache on, which is their default.

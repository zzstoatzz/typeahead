# zkv at corpus scale, 2026-11-10

One run of the [zkv-scale harness](https://tangled.org/zzstoatzz.io/typeahead/blob/main/experiments/zkv-scale/README.md) for issue 32: can zkv 0.1.2 on a local directory hold the actor corpus that Turso holds today?

On this evidence, yes for a local directory store. Size, memory, read latency, steady-state writes and a concurrent full scan were all comfortable. Two things need design attention: bulk loads outrun compaction, and a full compaction briefly doubles the disk used.

[Raw result lines](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/history/evals/zkv-scale-20261110/results.jsonl) hold every figure below.

## Run

- Flow run `fb2743a4-e39b-4ef2-8357-13e14eeab927`, harness commit `84a1f67`, 16:15 to 17:08 UTC.
- A disposable Hetzner cpx32 (4 vCPU, 8 GB RAM, 160 GB disk, Linux 6.8) from the `typeahead-ephemeral` pool.
- Input: search bundle `b1794291814-cp-91730d67b5` from R2, 6.42 GB, SHA-256 verified. 13,214,296 actor documents, 6.70 GB as JSON lines.
- No Turso, Fly or serving-slot access. Nothing came to HeavyPad or a laptop.
- The whole run took 52.6 minutes and peaked at 35.8 GB of scratch.

## Results

### Size

| | bytes |
|---|---|
| actor JSON loaded | 6.70 GB |
| logical keys and values, 45,278,811 entries | 8.61 GB |
| store settled, 4 KiB blocks | 9.42 GB |
| store settled, 64 KiB blocks | 9.27 GB |
| Turso checkpoint as SQLite (2026-11-05) | 7.14 GB |

The store is about 1.3 times the SQLite file. The secondary key ranges add 1.5 GB over the primary records: handles 0.73 GB, change order 0.54 GB, quality walk 0.15 GB, refresh queue 0.07 GB.

### Load

| | 4 KiB blocks | 64 KiB blocks |
|---|---|---|
| wall time | 17.5 min | 15.1 min |
| actors per second | 12,605 | 14,587 |
| commit p50 / p99 / max | 2.3 / 3.4 / 35 ms | 2.1 / 3.1 / 37 ms |
| time stalled on compaction | 717 s in 74 stalls | 615 s in 74 stalls |
| longest stall | 12.1 s | 10.7 s |
| peak RSS | 376 MB | 162 MB |
| disk peak | 10.0 GB | 10.0 GB |

Commits themselves stayed fast. The loader spent about two thirds of its time waiting: when more than 12 level-0 tables piled up it stopped writing until compaction caught up. zkv has no backpressure of its own, so that rule lives in the harness.

### Settling

Compacting to completion took 90 s and rewrote the whole store into one level. Disk peaked at 18.6 GB against 9.4 GB settled, because the old and new tables coexist until the job publishes.

### Memory and open time

| handle | fence stride | resident metadata | RSS after open | open time |
|---|---|---|---|---|
| writer or reader, 4 KiB blocks | 1 | 212 MB | 218 MB | 0.7–0.9 s |
| writer or reader, 4 KiB blocks | 32 | 60 MB | 63 MB | 0.7–0.8 s |
| reader, 64 KiB blocks | 1 | 66 MB | 69 MB | 0.35 s |

About 1,100 tables in every case.

### Reads after dropping the page cache

20,000 samples each, 4 KiB blocks.

| | p50 | p95 | p99 | max |
|---|---|---|---|---|
| actor by DID | 123 µs | 150 µs | 186 µs | 1.5 ms |
| handle lookup by range scan | 119 µs | 145 µs | 187 µs | 0.5 ms |

Fence stride 32 moved these by a few microseconds.

### Steady writes with a concurrent full scan

Ten minutes at 30 commits a minute of 60 actors each, plus a 3,000-actor burst every two minutes.

- Commit p50 1.4 ms, p99 2.0 ms, max 23 ms. No commit took longer than a second and no stalls occurred.
- A 3,000-actor burst took 84 to 126 ms in total.
- Slowest maintenance step: 40 ms.
- Meanwhile a second process scanned all 13.2 million actor records from a reader handle 51 times. Each scan took 10.2 to 11.9 s. None failed, and `error.Missing` never occurred.

### Restart storm

Re-sending 1,000,000 existing actors in commits of 400 took 62.8 s: 15,900 actors a second, with commit p99 at 4.1 ms and no stalls. For comparison, the production path through the Worker and Turso handled a few hundred events a batch at 4 to 7 s each during the 2026-11-09 incident.

### Counting everything

A scan of all 45.3 million entries took 19.4 s. Counts per key range were identical after the load, after settling, and after churn and storm.

## What this does not show

- **The change-order keys are not realistic.** A snapshot import stamps actors with the snapshot watermark and only later live updates carry a newer value (the load saw versions from 0, on 107 actors, to 1794293820), so the `u`, `t` and `r` ranges sort by DID within one stamp. Their sizes and counts are right; their ordering is not what a live system produces.
- **`r` and `i` are synthetic** stand-ins for the refresh and identity queues.
- **One machine, one run.** Local NVMe on a 4-vCPU VM. No second run was made to check variance.
- **No crash or power-loss test.** zkv's own docs cover SIGKILL only.
- **No object store.** Everything here is the directory store. Reads over R2 are a separate question (issue 33).
- **Ten minutes of churn.** Long-run behaviour, including how often a deep compaction recurs under real write volume, was not observed. After the storm a middle level held 0.5 GB above the 9.4 GB bottom level; the next compaction of it would rewrite the bottom level again.
- **Handle lookups returned a different DID first for 3,168 of 19,893 samples.** Several accounts share a handle value in the corpus; which values was not examined.

## What it means for the design

- A directory store at this size fits easily on the serving machine's disk and memory, with fence stride 32 or 64 KiB blocks keeping each handle near 60 MB.
- Budget twice the store's size in free disk for compaction.
- A bulk load or a rebuild needs backpressure against compaction, or a direct table-building path that zkv does not have yet.
- A reader in another process can take a full copy of the corpus in about 11 s while the writer keeps going, which is the operation the offline index build needs.

## Cleanup

The flow removed its scratch root (free space 149.4 GB before and 149.1 GB after; the C toolchain package it installed stays on the VM). The worker deleted the VM at 17:08:26 UTC. The temporary deployment `typeahead-zkv-scale/issue-32` was deleted afterwards; the flow run record is retained.

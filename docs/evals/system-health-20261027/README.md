# Typeahead health and storage audit — October 27, 2026

Production was inspected read-only. No Machine, route, memory size, schedule or
snapshot was changed. The new guarded-write and timing changes are local and
not deployed. The earlier admission experiment remains parked separately.

## Measurements

Fly's managed Prometheus is the source behind its Grafana dashboards. The
retained range is 04:14:21–06:14:21 UTC, at 30-second resolution. The current
serving slot changed at 06:05:14; comparisons below use only the subsequent
nine-minute interval, including startup/catch-up and qualification traffic.
They are not a clean representative traffic benchmark.

| Metric | Serving search | Ingester |
| --- | ---: | ---: |
| Mean CPU work | 0.024 cores of two | 0.049 cores of one |
| Maximum CPU work | 0.098 cores | 0.055 cores |
| CPU throttling | zero | zero |
| Mean CPU I/O wait | 0.287 cores | zero |
| Peak CPU I/O wait | 1.221 cores | zero |
| Memory PSI avg10 | zero in this interval | zero |
| Mean volume reads | 1.35 MB/s | zero local volume activity |
| Mean volume writes | 2.86 MB/s | zero local volume activity |

The search volume reached 93.4% busy and an estimated outstanding-I/O depth of
32.5 in one-minute counter windows. CPU work stayed low. This supports storage
waiting rather than CPU saturation. It does not prove a Fly device quota or
attribute every physical I/O to a particular request. Device timing/queue values
are counter-derived averages, not per-request latency percentiles.

The process snapshot has about 66 MiB anonymous RSS and 314 MiB file-backed RSS,
11 threads, a 1.56 GB overlay database, no freelist pages and a small WAL at that
instant. File-backed RSS is not a Zig heap allocation. A 25-second quiet sample
had no writes and about 2.2 MB of reads across workers. The ingester heartbeat
had zero ingest/deletion queues and zero backpressure.

Three clean serving sync passes took 67.36, 45.38 and 23.07 seconds. Their updated
actor batches took 9.19–26.79 seconds to apply, versus 0.30–1.00 seconds for the
corresponding Turso reads. The local apply span includes old actor reads, PDS
eligibility checks, key derivation, row/index updates and commit. Existing spans
do not separate those costs, so this audit does not pretend to have that split.

Public /stats embeds a materialized request series covering October 25 06:00
through October 27 06:00 UTC, despite the requested 24h display range. It contains
127,467 2xx, 528 4xx, 158 5xx and four 3xx answers. These are mixed endpoints,
mostly ingestion, and predate this cutover. The public search failure masking
identified in the RCA also means status counts understate search failures.
They cannot establish a current search success rate.

## Confirmed repeated work and local change

Sync rewinds its source watermark by 300 seconds for safe overlap. A bounded,
read-only source sample at 06:23:38 UTC found that all first 500 actors already
had the same source timestamp in actor_overlay. The source query had more rows;
this oldest-first sample is biased and is not a population replay fraction.
Timestamp equality alone does not prove every packed field is identical.

The prior projector uses INSERT OR REPLACE for each actor and each prefix,
including exact duplicates. Each prefix participates in the primary key,
updated-at index and, for additions, ranking index. Transaction batching already
exists; a 500-actor transaction still performs many individual row/index changes.

The local patch uses conflict updates guarded by equality of every stored
non-key field. Exact repeats perform no mutation. Changed blobs, hidden state,
scores, operations, timestamps and source ownership retain the old final row
state, including older incoming timestamps. NULL tombstones are compared safely.
The five-minute overlap, ranking, candidate caps and compaction policy stay fixed.

A three-trial file-backed SQLite mechanism benchmark uses 40,000 synthetic
prefix rows and 20,000 updates per arm. It is not a corpus capacity measurement:

| Scenario | Old mean | Guarded mean | Old WAL | Guarded WAL |
| --- | ---: | ---: | ---: | ---: |
| Identical replay | 57.0 ms | 16.3 ms | 2.71 MB | zero |
| 80% exact replay | 59.9 ms | 30.9 ms | 2.86 MB | 2.70 MB |
| All timestamps change | 63.9 ms | 66.8 ms | 2.97 MB | 2.97 MB |
| All scores change | 64.0 ms | 68.8 ms | 2.98 MB | 2.97 MB |

Changed rows incur a small comparison cost. Mixed writes still dirty many shared
pages, so row-change reduction does not imply proportional WAL savings.
The native suite passed 154 tests with six skipped; the Linux ReleaseSafe build
passed. Two real concurrent HTTP read/write tests passed natively; the final native
suite covers the actor guard and NULL tombstones. The final Linux executable
is also checked in a network-isolated, two-CPU / 1 GiB container; its test
result is retained in linux-http-tests.txt.
The benchmark compares full prefix states and post-compaction states across
implementations; a further 4,000 randomized actor/prefix operations matched after
all 2,000 paired state comparisons, including compaction and operator-source rows.

## Where the new timings go

The existing sampled search span now includes read_pool_wait_ms, prefix_ms,
merge_ms, hydrate_ms and encode_ms. Its duration begins before read-pool checkout;
the existing slow-search log still measures engine work after checkout.

sync.apply.updated records lock_wait_ms, projection_ms, old_row_ms, scope_ms,
overlay_ms, commit_ms and sqlite_changes. These nested phase times must not be
summed together with their enclosing projection_ms. sync.checkpoint records
checkpoint duration, busy, wal_frames and checkpointed_frames. No per-actor spans
or increased search sampling were introduced.

## Lessons from Stream and upstream JetStream V2

Source inspected: Stream 923f2eae50a49c98b47fdff26ec45849e7ac2d09 and upstream
JetStream 2b2bf447c3fd995de975dc21dc4d491347a6a6b6. Stream's declared semantic
parity pin is older than current upstream; their complete feature sets are not
assumed identical.

Both organize durable archive work around blocks, append-oriented segment files,
ordered durability and batched metadata. Stream's async flush queue is bounded;
its serving path pulls from a resident hot tail or bounded shared cold cache.
Its worker pipeline preserves per-DID ordering. Upstream's writer/metrics expose
admission wait, pending bytes/blocks, group commit and durable sequence state.
The detailed cost and ownership of work are visible rather than inferred solely
from total process CPU.

For Typeahead, the applicable sequence is: make replay cheap; measure real
projection/commit/checkpoint costs; bound background interference with serving;
then evaluate reducing prefix fanout or changing overlay storage against identical
results and crash/compaction behavior. A sequential delta log plus bounded
memtable/immutable sorted runs is a possible future experiment if random B-tree
updates remain dominant. It is not an implemented or selected architecture.
JetStream's range-replay archive cannot replace ranked prefix and actor point
lookups directly. This is also not a proposal to change Typeahead discovery from
the relay firehose to JetStream.

## Evidence and cleanup

fly-ranges.json and fly-storage.json retain exact PromQL and samples;
process-io.json, replay-sample.json, sync-phases.json and public-status.json retain
bounded readbacks. prefix-replay-benchmark.py reproduces the SQL mechanism test;
prefix-replay-benchmark.json and differential.json retain results.

Owner: this Typeahead health audit. Scratch was local scoped TemporaryDirectory
storage, budget 128 MiB, with no corpus copy. Every benchmark database was
removed on completion. Real HTTP test subprocesses were stopped. No trial Fly
server, persistent scratch dataset or recurring monitor was created.

Metric units and API: https://docs.fly.io/monitoring/metrics
Stream: https://tangled.org/zat.dev/stream/blob/923f2eae50a49c98b47fdff26ec45849e7ac2d09/docs/architecture.md
Upstream: https://github.com/bluesky-social/jetstream/blob/2b2bf447c3fd995de975dc21dc4d491347a6a6b6/internal/ingest/writer.go

# Native snapshot conversion

2026-10-08: implementation on `codex/native-compaction`, based on the compact
snapshot rollout branch. This does not change the serving format, ranking,
production pointer, or the scheduled Prefect deployment.

## Execution

```sh
just build-compactor
just test-compactor
just convert-quiet --source /path/index.db --manifest /path/manifest.json \
  --output /path/compact --build-id distinct-compact-build
```

`build-compactor` uses Zig 0.17.0, ReleaseSafe, and the repository's pinned SQLite
dependency. It installs `services/zig-out/lib/libtypeahead_compact.so` on Linux
or `.dylib` on macOS. `TYPEAHEAD_COMPACT_LIBRARY` selects an explicitly installed
library when the scripts are copied off-repository. Build outside HeavyPad when
possible; a cross-compiled Linux library can be copied there. Use a CPU target
compatible with the execution host.

The quiet recipe requires Linux CPU topology and package temperature sensors.
It selects one allowed CPU with the lowest maximum frequency (an efficiency core
on HeavyPad), sets nice level 19, and alternates SIGCONT/SIGSTOP over 20 ms periods
for a 20% execution duty cycle. Actual CPU share is tested with a real CPU-bound
child; scheduler timing is not an exact kernel quota. At a sampled package
temperature of 65°C it pauses until 60°C. A hot start waits before launching the
conversion. This covers hashing and SQLite maintenance as well as native packing.
Cancellation resumes and terminates the child; Linux parent-death signalling
kills it if the supervisor dies, including while the child is stopped. Missing
sensors/topology fail rather than silently running without thermal protection.

HeavyPad's user manager currently delegates only `memory pids`. It accepts
`CPUQuota` properties but its child cgroups have no `cpu.max`, so those settings
do not enforce a CPU limit. The quiet recipe deliberately does not depend on that
configuration or require root to change it. Direct library calls and the
low-level `build.py` CLI have no implicit limit. `convert.py` uses quiet supervision by default;
`--unlimited-cpu` is an explicit opt-out for controlled runs, including macOS
development. Other workloads can still heat the machine and hold quiet
conversion paused; this mode has no completion deadline.

The default service build installs the library alongside its executable, so the
Prefect rollout's existing build-then-`convert.py` path picks up both native
conversion and quiet supervision when it uses this revision. The current
deployment is not changed by this branch. A missing native library fails
explicitly, with no slow Python fallback. The focused `compact` build target
avoids compiling the servers when only the converter is needed.

## Implementation and compatibility

Python makes one C ABI call to pack the SQLite source, then one to verify the
generated map. Zig scans the original tables directly. Packing reuses key,
posting, and page buffers; it deduplicates opaque postings within each page,
stores root records contiguously, and writes a complete page per write. No row
crosses the Python/native boundary. Memory for normal row processing is bounded
by the maximum page extent, while the root directory grows with the page count.

Verification retains the production reader's structural validation and digest
check, then sequentially compares every decoded key and opaque value with a
fresh read-only SQLite scan. Python retains immutable-source checks, metadata
validation, preserved SQLite tables, fsync, and atomic directory publication.
The SQLite backup/drop/VACUUM path and native SHA implementations still perform
corpus-scale work; this is not a claim that conversion is free.

`reference_build.py` is the unchanged Python implementation, retained as a test
oracle. Differential tests require identical complete map and SQLite bytes,
not merely equivalent search responses. Coverage includes empty namespaces,
binary and empty values, long common prefixes, Unicode and NUL keys, posting
deduplication, restart/page boundaries, oversized records, and source type/bounds
failures. Rehashed corruption, truncation, and changed source contents must be
rejected. Existing conversion/publication tests exercise unchanged manifests,
preserved destinations, and publication failure behavior.

## Measurement

The comparison source is build `b1791408253-dac0`: 12,388,730 actors and
139,304,239 prefix lists. The Python rollout packed 150 million entries in
494.53 seconds and consumed 844.08 CPU-seconds for the complete conversion.
The native full-corpus run packed all 151,692,969 records in 68.05 wall-seconds /
60.85 CPU-seconds on efficiency CPU 16. Checksum comparison took 6.13 wall-seconds
/ 4.50 CPU-seconds; exhaustive verification took 90.04 wall-seconds / 65.49
CPU-seconds. Total map packing/checking: 164.21 wall-seconds, 130.84 CPU-seconds,
79.9 MiB peak RSS. The map is byte-for-byte identical: 6,789,828,608 bytes,
SHA-256 `273c523d138adc679a161cd17d7406f0228a370844e1c44462d58f2446f06487`.

The native full-corpus run exposed the ineffective user-systemd quota described
above. It was affinity-limited, not CPU-quota-limited. Do not label its timings
as a 20%-CPU run. It also excluded SQLite backup/VACUUM and outer conversion
manifest work, so 844/131 is not an end-to-end speedup claim. The historical
Python packing rate versus native packing suggests roughly an eightfold
reduction, but different core placement and cache conditions were not controlled.
The quiet launcher is separately exercised on real source rows with matched
Python/native end-to-end builds and real process/sensor integration checks.

Under the working quiet launcher, a one-million-record slice of that same source
(100,000 actors and 900,000 prefixes) took 138.02 wall-seconds / 25.67 CPU-seconds
through Python and 18.96 wall-seconds / 3.63 CPU-seconds through Zig, including
SQLite preservation and verification in both paths. Both files and manifests
match exactly: 7.1× less CPU time in this matched sample. Total child CPU usage
was 31.81 seconds over 172.87 seconds including fixture creation, or 18.4% of one
CPU. The thermal guard paused and resumed during unrelated host spikes; sampled
package temperature peaked at 83°C, so this is not a claim that the whole host
stayed below 65°C. Sensor polling reacts to heat rather than preventing every
instantaneous excursion.

Raw results: [full corpus](evals/native-compaction/full.json) and
[quiet subset](evals/native-compaction/quiet-subset.json). Reproduce with
`bench_native.py SOURCE NEW_OUTPUT --expected-sha256 HASH` and
`quiet.py -- python3 bench_subset.py SOURCE NEW_OUTPUT`, respectively.

Validation: 166 Zig service tests passed (four platform/fixture skips), ten
Python conversion/publication/differential tests passed, and all three Linux
quiet-mode integration tests passed on HeavyPad. The default conversion CLI
also produced a verified fixture under supervision at a sampled peak of 50°C.

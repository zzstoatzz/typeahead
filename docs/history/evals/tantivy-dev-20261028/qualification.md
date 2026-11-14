# Development deployment qualification

Qualification recorded October 29, 2026. The parallel service is publicly available
on two shared CPUs and 1 GiB; remaining gates below apply to any later cutover.
Production cutover is outside this task.

| Requirement | Evidence and current status |
| --- | --- |
| Tantivy 0.27 with canonical Zig normalization | Pinned Cargo.lock, normalizer adapter tests, build receipts and replacement image; implemented |
| Independent dev ownership and storage | `resources.json`, topology, inventory; one owned dev machine and separate 40 GiB volume |
| Existing serving/rollback/ingester preserved | `protected-latest.json`; IDs, instances, images, guests, mounts and production route unchanged at the final check |
| Refresh ownership preserved | Separate dev app, no managed-slot or routing writes; home control-plane ownership documented; cloud-builder project inventory is empty in `hetzner-latest.json` |
| Corpus stays cloud-to-cloud | Pinned R2 GET capabilities on Fly, cloud-local source/export/import, fixed Turso SELECT replay; only code, metadata and bounded receipts cross operator connection |
| Correctness and lifecycle pilot | `cloud-pds-fix-pilot.log`: 12 backend groups, 8 source-PDS checks, 5 adversarial tests, 9 exporter and 10 replay tests passed |
| Actual source failure handled without losing actors | First-attempt failure/classification, real source witness recovery, failed-index retirement receipts; corrected clean full import admitted all actors |
| Full source admission | Passed: 12,816,605 actors, verified export count/hash/footer, merged index, `import_complete` and `full_source`, temporary source retired; `full-admission-and-replay-start.json` |
| Source replay and freshness | Passed after retained-tombstone source fix, 15 source tests and actual Rust integration. `source-progress.json`: 293 applied/1,075 replayed actors in five fully contained live-load cycles; 469 applied across entire load, zero errors since baseline. Policy `turso-current-state-cutoff-v1`, fresh committed watermark, no stale/partial overrides |
| Final serving shape and public dev URL | Passed: two shared CPUs/1 GiB, restart on admitted data, HTTPS search/health/readiness. API `https://typeahead-tantivy-dev.fly.dev`; production-UI proxy `https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev`; `ui-verification.json` checks homepage byte identity, health/search and XRPC alias equivalence, without visual QA |
| Real-corpus semantic checks | Passed: `real-qualification.json`, 558 checks/29 requests, source verified through three indexed PK SELECTs covering 188 rows/170,181 bytes; positive/negative real-PDS pair and Justin witness. No inconclusive checks. This is not a complete broad-query top-K oracle |
| Strong load smoke | Passed normal-rate correctness: `load-results.json`, 4,749 requests/zero failures; queue-inclusive p99 warm 12.936 ms and live replay 13.004 ms. Diverse 512-prefix p99 390.279 ms exceeds 100 ms; first-touch remains a gap. Overload: 147 expected rejections/109 accepted, recovery passed |
| Resource evidence | `resource-summary.json` covers entire load: peak RSS 695,300,096 bytes, zero swap/OOM, 6,617 major faults and about 0.60% I/O pressure. CPU/cgroup counters unavailable. VM-wide counters include replay/telemetry; RSS includes mapped pages |
| Standard repository benchmarks | `ranking-bench.log` passed all structural and named-query parity checks against explicit dev target. `engine-bench.json` completed 44 queries/three runs without stress: operator-to-dev warm p50/p95 43/67 ms, varied 44/70 ms. These are not physical cold-disk measurements. Broad top-10 overlap with Bluesky averaged 47%, below the repository's 75% aspiration |
| Durable full-corpus restart | Passed final-size restart on admitted index with committed cursor and subsequent real source mutation progress; `final-size-public-start.json`, `load-results.json`, `source-progress.json` |
| Cleanup and operational handoff | Passed: `lifecycle-final.json` confirms one started/uncordoned dev machine, its 40 GiB volume, load helper absent, source payload retired and sampler stopped. `controller-cleanup.json` confirms controller app token revoked/private file deleted, runtime unchanged. Admitted dev service/index retained for user testing; README/topology match deployed state |

## Boundaries before any later cutover

The real-corpus helper verifies returned rows and known regression witnesses;
it is not a complete expected top-K oracle. The synthetic suites provide
independent expected results for adversarial eligibility, ranking and mutation
cases. First-touch timings after restart do not establish a provider-physical-
disk cold state. Five minutes of live replay does not establish long-term merge
or rank-churn behavior.

Source timestamps have second-resolution conflicts, 300-second bounded lookback
and finite tombstone retention. Current live rows suppress retained tombstones only
within the replay cutoff. A late equal-version restore after an already committed
deletion still returns 409. Arbitrarily backdated writes and cross-stream races
beyond the lookback are not solved. Native moderation-refresh integration remains a separate
cutover gate. PDS filters/preferences can use recorded canonical hosts only;
the full import must report missing and unscopable source coverage explicitly.
Engine support does not repair missing source metadata. These limits must stay
visible even if development smoke tests pass.

# Live compact performance check — 2026-10-17

Compact production is healthy and faster than retained legacy in this small
paired test. This does not establish a sustained production-tail or load claim.

- Full approved Worker smoke suite: exit 0, all checks passed, including ranking,
  moderation, admin CRUD and Turso plan checks.
- Public freshness: healthy, compact-b1792141282-189b, replica lag 95 seconds at
  initial check. Both backends report 12,560,923 actors from the paired snapshot.
- Paired direct HTTP test: 10 queries × 5 rounds × 2 backends, alternating order,
  sequential requests with 100ms pacing. All 100 succeeded; all 50 paired JSON
  responses matched exactly. Round zero warms each endpoint; 40 measurements per
  backend remain. Includes network/TLS overhead, not isolated engine timing.
- Legacy median 162.73ms, p95 295.33ms; compact median 139.90ms, p95 222.00ms:
  14.0% lower median, 24.8% lower p95. p95 is sorted index floor(.95*(n-1)).
- Existing public benchmark, --quick --no-stress: 10 queries, one run. Median of
  query measurements: warm 55.3ms, cache-busting cold 427.35ms, Bluesky 135.85ms.
  Cold max 2240.5ms. Cache state is the benchmark's request design, not independently
  verified cache-hit headers. Warm is faster than Bluesky; cold is slower.
- Mean Bluesky overlap 43%, no complete misses, field completeness 96–99%.
  This quick comparison is not the full ranking acceptance gate.

Logfire was queried in project typeahead. From 18:30 UTC through cutover, 92
legacy search spans had p50 4.30ms and p95 15.28ms. No compact service records
were returned after cutover. Fly secret names and an in-machine presence-only
check confirmed the compact machine lacks LOGFIRE_TOKEN. Thus Logfire cannot
establish compact latency or its error rate. Later legacy probes appeared there,
confirming that the query path works. Post-cutover records showed two ingester
ReadFailed events at 20:00:52 and 20:20:46 UTC; ingestion continued afterward and
public freshness was healthy. Zero reported legacy search errors is not evidence
of zero compact errors. Restore compact telemetry with an availability-preserving
transition; do not restart the sole live backend just to enable it.

Raw evidence is in live-compact-perf-20261017/: smoke log, public benchmark report,
paired request script/results and status snapshots. Benchmark results retain the
first run and errors; no failing samples were discarded. There were no routing,
runtime configuration or source changes during this check.

README review: no runtime/product behavior changed in this task, so its live
compact rollout status remains valid. The proposed PDS UI/backend changes are in
../plans/pds-homepage-rollout.md and are explicitly not shipped.

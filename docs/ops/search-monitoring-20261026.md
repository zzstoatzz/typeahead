# Search monitoring repair — October 26, 2026

Production moved to `typeahead-search-refresh`, but the overlay-sync alert still
selected the retired `typeahead-search-compact` service. Both managed refresh apps
also lacked their Logfire write credential. Missing spans therefore had two causes;
changing the alert's service name alone would not restore monitoring.

The existing Typeahead project token was recovered privately from the ingester,
preserved in the local encrypted store, and staged on both managed apps. No token
was printed or rotated. The installer now rejects a missing telemetry credential
before installation. `scripts/snapshot_refresh/provision_telemetry.py` repeats the
staging operation without restarting running Machines.

The first public freshness read was healthy. Later live logs showed persistent
`TlsInitializationFailed` responses from Turso and a stalled overlay watermark.
Zig 0.17's HTTP client retained the certificate validation time from its first HTTPS
request. The repair refreshes that time under the client's existing request mutex,
while leaving the first request's CA-bundle initialization intact. Real HTTPS tests
on macOS and x86-64 Linux reproduce stale-time rejection and then recover without
credentials or disabled certificate checks.

## Deployed repair

Native source: `503b8b1`. Image:
`registry.fly.io/typeahead-search-refresh@sha256:37fa2b5ed4cf21c63a1e75fefd0f48d001c2cf3d0635f5ee16441c40cece6525`.
The serving Machine `85379da4636338` was explicitly stopped and updated for this
repair, retaining its exact configuration except image, its 20 GB volume
`vol_r685k90m3x97opj4`, installed snapshot and overlay. The installer reused the
cached admitted generation; it did not rebuild or download the corpus. Readiness
returned at 18:35:26 UTC. This was a serving-process restart, not a claim of zero
downtime. No route, snapshot pointer, ranking policy or resource size changed.

The cloud publisher's source and live `INDEX_REFRESH_IMAGE` both select this image.
The active `0 6 */2 * *` UTC schedule is preserved; October 27/29/31 queued runs
have empty job-variable overrides and inherit the corrected deployment image.

The transport marks routed `/health` requests. Native health spans expose
`production_probe=true`. The alert selects the most recently probed production
slot and requires an error-free, non-skipped `sync.incremental` span from that
same service. A missing routed probe also produces a finding. Existing channels,
five-minute evaluations, 30-minute window and one-minute watermark are preserved.
The [runbook](../plans/automatic-snapshot-refresh.md#search-telemetry) records the
cutover/probe timing limitation and independent public freshness check.

A real workerd request to the retired slot reproduced HTTP 500 from an uncaught
origin fetch exception. The transport now returns 503 `SearchBackendUnavailable`
for origin connection errors and timeouts. This explains a reproducible failure
mode, not the exact historical request reported by Phi.

## Verification

The first recovered incremental pass completed at 18:40:20 UTC: 4,488 actor
updates, 191 unsearchable clearances and 891 deletions, with status `OK` and no
sync errors. Public freshness returned HTTP 200 with 20 seconds lag immediately
afterward. Routed production health and completed sync spans are visible in
Logfire under `typeahead-search-refresh`.

The exact alert query was exercised in Logfire's SQL engine with production,
rollback-only, missing-probe, errored and skipped-pass fixtures. Only the production
success case returned no finding. No fixture telemetry was written. Live query,
health and scheduled-evaluation readbacks are in the
[receipt](../evals/search-monitoring-20261026.json).

Native tests, the real HTTPS regression on both target platforms, four installer
checks, all 94 Worker tests, TypeScript, lint, real workerd/R2 routing checks and
public smoke passed. The smoke run skipped credential-dependent admin/query-plan
checks; no ranking or index format changed. The live workerd test also verifies
503 classification against the deliberately unavailable empty managed slot.
Cold requests can still exceed the transport deadline during overlay catch-up;
this repair does not establish a new latency guarantee.

## 20:00 Chicago freshness alert

A later alert on October 26 was a transient serving timeout. Cloudflare recorded
`/health` returning 503 after 3,211 ms at 01:00:56.536 UTC on October 27
(20:00:56 Chicago, October 26), request `768aa3559b6697a4a034ce084dd4eae5`.
The next recorded health request returned 200 at 01:01:02.956 UTC after 2,672 ms;
the following one took 137 ms. The cron logged `freshness_stale` at 01:00:59.750
and its Discord webhook returned 204 at 01:01:00.007.

The overlay sync ran from 01:00:44.387 through 01:01:10.676 with status OK:
461 updates, 107 clearances and 44 deletions. Search logs in that interval include
an 8,393 ms query. Native health handlers that reached execution remained fast
(about 0.4–12 ms). The accept loop's inline fallback can execute slow search on
the accepting thread; queue starvation is consistent with these observations,
but there is no accept-to-handler timing span proving that specific mechanism.
There was no restart or new TLS failure in the inspected interval.

This alert does not establish stale data. Missing health/index/probe responses
leave lag and snapshot unknown; the notifier currently prints an unavailable probe
as `FAILING`. It checks state transitions hourly, so recovery can remain unannounced
until the next cron even when the public endpoint is already healthy. Serving
latency, health-request isolation and notification semantics remain follow-up
work. No code, thresholds, schedule or deployment changed during this investigation.
The README's deployment description therefore remains unchanged.

Evidence: Cloudflare Workers Observability query `73tzjqcet2ahf1vx5cxo8l4o`
(transport, 00:59:40–01:02:00 UTC) and `jd25lb006rwqcgrggg0ma94u` (public
Worker); native Logfire `http.health`, `search` and `sync.incremental` records in
00:58:30–01:02:10 UTC. No extra production benchmarks were run.

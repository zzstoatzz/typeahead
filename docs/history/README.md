# History

Nothing in this directory describes the running system. These are the plans,
rollouts, retrospectives, evaluations, research and drafts written while
typeahead ran on Turso, the native engine and the Turso-fed Tantivy slots, and
while it moved to store slots. Machine IDs, commands, schedules and prices in
them were true on their dates only. For the system as it runs, start at the
[docs index](../README.md).

Each line gives a document or a set of evidence files, what it was, and the
date it was first committed at its earlier path. Loose receipt files are listed as one set.

## the system before the store slots

- [architecture.md](architecture.md): architecture of the Turso-backed system: ingester, Turso, offline index builds, Tantivy slots fed from Turso. 2026-03-18, last revised November 2026.
- [operations.md](operations.md): operations guide for that system, with the rollout notes for statistics in D1, slot control and Turso retirement. 2026-05-26, last revised 2026-11-12.
- [discovery-and-identity.md](discovery-and-identity.md): how the ingester discovered accounts and resolved handles through Turso's identity queue. 2026-07-28.
- [costs-through-20261024.md](costs-through-20261024.md): the cost record and its changelog, formerly `COSTS.md`. 2026-06-17 to 2026-10-24.
- [index-bridge.md](index-bridge.md): design and protocol of the index bridge, written before anything ran it. 2026-11-10.
- [slot-control.md](slot-control.md): design and rollout of the control database that carries policy and index requests to slots. 2026-11-12.
- [turso-retirement.md](turso-retirement.md): inventory, parity checks and rollout order for moving the Worker's reads off Turso. 2026-11-12.
- [ops/store-slot-operations.md](ops/store-slot-operations.md): the first route runbook for store slots, written while slot B was being loaded. 2026-11-12.

## designs, plans and research

- [collection-discovery.md](collection-discovery.md): collection discovery: retiring the hardcoded allowlist. 2026-08-18.
- [did-prefix-search.md](did-prefix-search.md): DID-prefix search (`i:` key family). 2026-08-17.
- [hrana-v3-cursor-plan.md](hrana-v3-cursor-plan.md): plan: hrana v3 cursor source for the indexer (v2 — corrected after spec re-read). 2026-05-24.
- [i18n-cjk-search.md](i18n-cjk-search.md): i18n: Japanese / CJK search. 2026-05-26.
- [pds-identity-repair.md](pds-identity-repair.md): PDS identity coverage. 2026-10-20.
- [pds-repair-cleanup-20261020.md](pds-repair-cleanup-20261020.md): PDS repair cleanup — October 20, 2026. 2026-10-20.
- [prioritized-enrich-plan.md](prioritized-enrich-plan.md): prioritized enrichment — plan and implementation record. 2026-05-28.
- [private-directories.md](private-directories.md): private directories: typeahead stays the public-context service. 2026-09-18.
- [turso-research.md](turso-research.md): turso capabilities — features we haven't been using. 2026-05-24.
- [turso-usage-analytics.md](turso-usage-analytics.md): turso usage analytics — reading the read budget. 2026-05-24.

## plans and rollouts

- [plans/automatic-snapshot-refresh.md](plans/automatic-snapshot-refresh.md): automatic snapshot refresh. 2026-10-25.
- [plans/compact-production-cutover.md](plans/compact-production-cutover.md): compact production cutover. 2026-10-16.
- [plans/embedded-store-service.md](plans/embedded-store-service.md): embedded-store service. 2026-11-10.
- [plans/label-stream.md](plans/label-stream.md): label stream. 2026-10-31.
- [plans/lifecycle-cleanup.md](plans/lifecycle-cleanup.md): lifecycle cleanup. 2026-11-04.
- [plans/pds-homepage-rollout.md](plans/pds-homepage-rollout.md): PDS filtering rollout plan. 2026-10-18.
- [plans/snapshot-compaction-rollout.md](plans/snapshot-compaction-rollout.md): snapshot compaction rollout. 2026-10-08.
- [plans/stats-off-turso.md](plans/stats-off-turso.md): request and traffic statistics off Turso. 2026-11-10.
- [plans/tantivy-cutover-readiness.md](plans/tantivy-cutover-readiness.md): Tantivy cutover readiness. 2026-10-29.
- [plans/worldwide-serving.md](plans/worldwide-serving.md): serving search from several regions. 2026-11-01.

## operational records

- [ops/archive-pds-candidate-20261030/](ops/archive-pds-candidate-20261030/): full archive PDS join candidate, 50 files. 2026-10-30.
- [ops/archive-pds-join-pilot-20261030/](ops/archive-pds-join-pilot-20261030/): archive PDS join pilot, 3 files. 2026-10-30.
- [ops/archive-pds-pilot-20261030/](ops/archive-pds-pilot-20261030/): cloud archive PDS pilot — October 30, 2026, 15 files. 2026-10-30.
- [ops/automatic-refresh-20261025.md](ops/automatic-refresh-20261025.md): automatic snapshot refresh — October 25, 2026. 2026-10-25.
- [ops/display-name-recall-20261028/](ops/display-name-recall-20261028/): multiword display-name recall incident, 3 files. 2026-10-28.
- [ops/identity-repair-20261028/](ops/identity-repair-20261028/): invalid identity containment and repair, 1 files. 2026-10-28.
- [ops/invalid-handle-claims-20261028.md](ops/invalid-handle-claims-20261028.md): invalid handle claims in live search. 2026-10-28.
- [ops/lifecycle-cleanup-20261105/](ops/lifecycle-cleanup-20261105/): lifecycle qualification, 85 files. 2026-11-04.
- [ops/pds-cloud-pilot-20261030/](ops/pds-cloud-pilot-20261030/): cloud PDS pilot, 2 files. 2026-10-30.
- [ops/pds-cloud-stage100-20261030/](ops/pds-cloud-stage100-20261030/): cloud PDS repair: 100-account stage, 2 files. 2026-10-30.
- [ops/pds-repair-retirement-20261030/](ops/pds-repair-retirement-20261030/): standalone PDS repair experiment retired, 2 files. 2026-10-30.
- [ops/poller-readiness-20261104/](ops/poller-readiness-20261104/): hourly source-poller readiness failures, 62 files. 2026-11-04.
- [ops/production-recovery-20261031/](ops/production-recovery-20261031/): October 31 production recovery, 26 files. 2026-10-31.
- `ops/search-*-2026102{4,5}.json`: resource and deployment receipts from the isolated search cutover, 7 files. 2026-10-24 to 2026-10-25.
- [ops/search-cutover-20261025.md](ops/search-cutover-20261025.md): isolated search rollout and rollback. 2026-10-25.
- [ops/search-cutover-result-20261025.md](ops/search-cutover-result-20261025.md): search cutover execution. 2026-10-25.
- [ops/search-monitoring-20261026.md](ops/search-monitoring-20261026.md): search monitoring repair — October 26, 2026. 2026-10-26.
- [ops/sync-hardening-release-20261026.md](ops/sync-hardening-release-20261026.md): sync hardening release — October 26, 2026. 2026-10-27.
- [ops/tantivy-live-route-rehearsal.md](ops/tantivy-live-route-rehearsal.md): isolated routing rehearsal with live backends. 2026-10-30.
- [ops/test-resource-retirement-20261031/](ops/test-resource-retirement-20261031/): Typeahead test resource retirement — October 31, 2026, 9 files. 2026-10-31.

## retrospectives

- [retros/2026-05-23-standard-site-backfill.md](retros/2026-05-23-standard-site-backfill.md): retrospective: 2026-05-23 standard.site backfill and production degradation. 2026-05-24.
- [retros/2026-05-25-typeahead-rearchitecture-notes.md](retros/2026-05-25-typeahead-rearchitecture-notes.md): Typeahead Re-Architecture Notes. 2026-05-26.
- [retros/2026-06-12-ingester-wedge-and-recovery.md](retros/2026-06-12-ingester-wedge-and-recovery.md): retrospective: 2026-06-12 → 06-15 ingester wedge, recovery, and throughput root cause. 2026-06-14.
- [retros/2026-07-28-search-replica-sync-wedge.md](retros/2026-07-28-search-replica-sync-wedge.md): retrospective: 2026-07-28 → 07-31 search replica sync wedge. 2026-07-31.
- [retros/2026-08-28-search-volume-full-again.md](retros/2026-08-28-search-volume-full-again.md): retrospective: 2026-08-25 → 08-28 search volume full, empty results for ~3 days. 2026-08-28.
- [retros/2026-09-03-ingester-panic-hang-and-turso-saturation.md](retros/2026-09-03-ingester-panic-hang-and-turso-saturation.md): 2026-09-03 — ingester alive-but-dead for 22 h; then a viral burst saturated Turso through the worker. 2026-09-04.
- [retros/2026-09-05-ingester-cpu-quota.md](retros/2026-09-05-ingester-cpu-quota.md): September 5: ingester CPU quota and misleading dashboard. 2026-09-09.
- [retros/2026-09-05-ingester-recovery-closeout.md](retros/2026-09-05-ingester-recovery-closeout.md): ingester event-loss mitigation and recovery. 2026-09-09.
- [retros/2026-09-05-system-performance-review.md](retros/2026-09-05-system-performance-review.md): Typeahead performance, resources, and cost — September 5. 2026-09-09.
- [retros/2026-09-09-ingester-panic-hang-recurrence.md](retros/2026-09-09-ingester-panic-hang-recurrence.md): 2026-09-09: the ingester panicked and did not die, again. 2026-09-09.
- [retros/2026-10-26-search-timeouts-and-freshness-alerts.md](retros/2026-10-26-search-timeouts-and-freshness-alerts.md): search timeouts and misleading freshness alerts — October 26, 2026. 2026-10-26.
- [retros/2026-10-31-search-expiry-outage.md](retros/2026-10-31-search-expiry-outage.md): search outage after snapshot coverage expired. 2026-10-31.
- [retros/2026-11-02-tantivy-slot-expired-again.md](retros/2026-11-02-tantivy-slot-expired-again.md): Tantivy slot expired after one missed refresh. 2026-11-02.
- [retros/README.md](retros/README.md): retros. 2026-06-14.

## evaluations

- [evals/bounded-overlay.md](evals/bounded-overlay.md): bounded overlay lookup review — September 5, 2026. 2026-09-09.
- [evals/collector-cpu-20261112/](evals/collector-cpu-20261112/): collector CPU, 2026-11-12, 11 files. 2026-11-12.
- `evals/compact-*, evals/xrpc-compact-qualification.json`: loose receipts, logs and scripts from the compact snapshot qualification and rollout, 41 files. 2026-10-09 to 2026-10-19.
- [evals/compact-cancel-fixed-cold/](evals/compact-cancel-fixed-cold/): cold benchmark and diagnostics after the compact cancel fix, 8 files. 2026-10-17.
- [evals/compact-cancel-fixed-warm/](evals/compact-cancel-fixed-warm/): warm benchmark after the compact cancel fix, 2 files. 2026-10-17.
- [evals/compact-deployed-compat-failure/](evals/compact-deployed-compat-failure/): trial log of a compact reader compatibility failure, 3 files. 2026-10-17.
- [evals/compact-inline-warm/](evals/compact-inline-warm/): warm benchmark of inline compact scheduling, 3 files. 2026-10-16.
- [evals/compact-logfire-repair/](evals/compact-logfire-repair/): probes and smoke output from the compact Logfire repair, 7 files. 2026-10-19.
- [evals/compact-native-reload/](evals/compact-native-reload/): reload latency protocol, scripts and results for the native engine, 9 files. 2026-10-17.
- [evals/compact-production-cutover/](evals/compact-production-cutover/): receipts from the compact snapshot production cutover, 12 files. 2026-10-17.
- [evals/compact-scheduled-publication-20261019/](evals/compact-scheduled-publication-20261019/): October 19 scheduled compact publication, 18 files. 2026-10-19.
- [evals/display-name-recall-20261028/](evals/display-name-recall-20261028/): candidate recall investigation, 107 files. 2026-10-28.
- `evals/edge-*-20261020.json`: edge latency benchmarks before and after transport placement, 4 files. 2026-10-20.
- [evals/edge-latency-20261020.md](evals/edge-latency-20261020.md): cold edge latency, October 20, 2026. 2026-10-20.
- [evals/first-request-latency-20261104/](evals/first-request-latency-20261104/): first-request latency samples: minification, transport placement, route reads, 6 files. 2026-11-04.
- [evals/flat-prefix-compaction.md](evals/flat-prefix-compaction.md): snapshot compaction. 2026-10-08.
- [evals/handle-sidecar-20261028/](evals/handle-sidecar-20261028/): handle sidecar evaluation, 29 files. 2026-10-28.
- [evals/image-renderer-split-20261104/](evals/image-renderer-split-20261104/): samples from splitting image rendering into its own Worker, 5 files. 2026-11-04.
- [evals/live-compact-perf-20261017.md](evals/live-compact-perf-20261017.md): live compact performance check — 2026-10-17. 2026-10-18.
- [evals/live-compact-perf-20261017/](evals/live-compact-perf-20261017/): data behind the live compact performance check, 7 files. 2026-10-18.
- [evals/native-compaction/](evals/native-compaction/): native snapshot compaction results, 2 files. 2026-10-08.
- `evals/pds-compact-results-20261020.json, evals/pds-scope-results.json`: results of the PDS scope experiments, 2 files. 2026-10-20.
- [evals/pds-full-corpus-20261020/](evals/pds-full-corpus-20261020/): receipts from the full-corpus PDS scope rollout, 22 files. 2026-10-20.
- [evals/pds-homepage-20261020.md](evals/pds-homepage-20261020.md): PDS homepage review — October 20, 2026. 2026-10-20.
- [evals/pds-parser-cutover-20261020.md](evals/pds-parser-cutover-20261020.md): parser-fix production cutover. 2026-10-20.
- [evals/pds-parser-cutover-20261020/](evals/pds-parser-cutover-20261020/): probes and smoke logs from the parser-fix cutover, 6 files. 2026-10-20.
- [evals/pds-replay-response-fix.md](evals/pds-replay-response-fix.md): SQL statement failures during PDS policy replay. 2026-10-20.
- [evals/pds-replay-response-fix/](evals/pds-replay-response-fix/): data behind the PDS policy replay fix, 3 files. 2026-10-20.
- [evals/pds-scope.md](evals/pds-scope.md): small-PDS search experiment — 2026-10-08. 2026-10-20.
- [evals/perf-goal-20261027/](evals/perf-goal-20261027/): performance goal completion audit, 400 files. 2026-10-27.
- [evals/production-latency-20261104/](evals/production-latency-20261104/): production latency samples, 6 files. 2026-11-04.
- [evals/ranking-page-20261020.md](evals/ranking-page-20261020.md): ranking page refresh — October 20, 2026. 2026-10-20.
- [evals/rca-20261026/](evals/rca-20261026/): evidence for the October 26 search timeout analysis, 9 files. 2026-10-26.
- [evals/refresh-performance-20261105/](evals/refresh-performance-20261105/): refresh performance evaluation, 68 files. 2026-11-05.
- `evals/search-monitoring-20261026.json, evals/search-retirement-20261025.json`: receipts for the search monitoring repair and the native search retirement, 2 files. 2026-10-25 to 2026-10-26.
- [evals/shadow-latency-20261111/](evals/shadow-latency-20261111/): shadow search latency, 2026-11-11, 24 files. 2026-11-11.
- `evals/startup-*-2026102{4,5}.{json,log}`: receipts from the startup, parallel search and first managed refresh qualification, 50 files. 2026-10-24 to 2026-10-25.
- [evals/startup-admitted-candidate-20261024.md](evals/startup-admitted-candidate-20261024.md): verified startup and parallel search candidate. 2026-10-24.
- [evals/startup-buffer-20261024.md](evals/startup-buffer-20261024.md): startup validation buffering experiment. 2026-10-24.
- [evals/startup-fresh-candidate-20261025.md](evals/startup-fresh-candidate-20261025.md): fresh full-corpus candidate qualification. 2026-10-25.
- [evals/sync-hardening-release-20261026/](evals/sync-hardening-release-20261026/): search sizing observations — October 27, 2026 UTC, 34 files. 2026-10-27.
- [evals/system-health-20261027/](evals/system-health-20261027/): Typeahead health and storage audit — October 27, 2026, 12 files. 2026-10-27.
- [evals/tantivy-dev-20261028/](evals/tantivy-dev-20261028/): isolated Tantivy dev backend, 150 files. 2026-10-29.
- [evals/tantivy-qualification-20261029/](evals/tantivy-qualification-20261029/): matched engine qualification evidence — October 29, 2026, 50 files. 2026-11-02.
- [evals/worker-request-timing-20261104/](evals/worker-request-timing-20261104/): worker request timing samples, 6 files. 2026-11-04.
- [evals/worker-startup-cause-20261104/](evals/worker-startup-cause-20261104/): worker startup delay trials, 10 files. 2026-11-04.
- [evals/zkv-scale-20261110/](evals/zkv-scale-20261110/): zkv at corpus scale, 2026-11-10, 2 files. 2026-11-10.
- `evals/{bounded-overlay,canary,full-overlay,live-shadow,production-after-shadow}*.json`: receipts for the bounded overlay lookup review, 12 files. 2026-09-09.

## archive: the native engine

- [archive/README.md](archive/README.md): archived designs and operations. 2026-10-31.
- [archive/architecture-diagram.html](archive/architecture-diagram.html): diagram of the native-engine system, HTML source. 2026-10-31.
- [archive/architecture-diagram.png](archive/architecture-diagram.png): the same diagram as a PNG. 2026-10-31.
- [archive/architecture-diagram.svg](archive/architecture-diagram.svg): the same diagram as an SVG. 2026-10-31.
- [archive/architecture-native.md](archive/architecture-native.md): historical native architecture. 2026-10-31.
- [archive/cost-scalability.md](archive/cost-scalability.md): typeahead — cost & scalability. 2026-10-31.
- [archive/index-serving-impl-plan.md](archive/index-serving-impl-plan.md): index-backed serving + overlay — implementation plan (for review). 2026-10-31.
- [archive/native-compaction.md](archive/native-compaction.md): native snapshot conversion. 2026-10-31.
- [archive/native-operations-through-20261031.md](archive/native-operations-through-20261031.md): operations / edge gotchas. 2026-10-31.
- [archive/native-snapshot-refresh.md](archive/native-snapshot-refresh.md): automatic isolated snapshot refresh. 2026-10-31.
- [archive/pds-filtering-history.md](archive/pds-filtering-history.md): how PDS filtering changed. 2026-10-31.
- [archive/pds-live-write-repair.md](archive/pds-live-write-repair.md): retired PDS live-write repair experiments. 2026-10-31.
- [archive/replica-removal-plan.md](archive/replica-removal-plan.md): plan: remove the Turso replica from the search box. 2026-10-31.
- [archive/search-replica-research.md](archive/search-replica-research.md): research: does the search box need the Turso replica? 2026-10-31.
- [archive/typeahead-index-design.md](archive/typeahead-index-design.md): typeahead index design. 2026-10-31.

## notes

- [notes/ingester-oom-firehose-widening.md](notes/ingester-oom-firehose-widening.md): incident: ingester OOM after widening firehose. 2026-03-19.

## drafts

- [drafts/architecture-removal/](drafts/architecture-removal/): replica-removal preview, 4 files. 2026-09-09.
- [drafts/launch-replay/](drafts/launch-replay/): interactive replay of launch traffic, unpublished draft, 9 files. 2026-09-09.
- [drafts/typeahead-sequel-evidence.md](drafts/typeahead-sequel-evidence.md): sequel evidence and operational handoff. 2026-09-09.
- [drafts/typeahead-sequel.html](drafts/typeahead-sequel.html): rendered draft of the sequel blog post. 2026-09-09.
- [drafts/typeahead-sequel.md](drafts/typeahead-sequel.md): typeahead at the race. 2026-09-09.

## blog assets

- `blog-assets/*.{png,svg,py,mjs}`: images and render scripts for the "pendulum backswing" blog post, 10 files. 2026-05-26.
- [blog-assets/README.md](blog-assets/README.md): blog assets — "the pendulum backswing". 2026-05-26.
- [blog-assets/typeahead-sequel/](blog-assets/typeahead-sequel/): Typeahead sequel assets, 29 files. 2026-09-09.


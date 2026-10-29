# Tantivy cutover readiness

Owner: `codex/tantivy-cutover-readiness`. Started October 29, 2026.
Production routing, serving/rollback machines and live ingester remain protected.
The dev API and production-identical frontend remain parallel to production.
This task prepares evidence and a later cutover proposal; it does not authorize promotion.

## Qualification evidence

October 29: the expiry-v4 candidate passed actual Linux HTTP source, policy,
crash/restart, expiry-coverage and exact boost tests under a two-CPU/1-GiB
container limit. A separate 5,000-profile replay experiment preserved all stored
documents and immutable segment files across 15,000 version-only observations;
real source churn and sweep throughput remain unmeasured.

The [cloud operational pilot](../evals/tantivy-dev-20261028/qualification-cloud-pilot.json)
passed on independently observed two-shared-CPU/1-GiB machines with separate
20-GiB disks and load generation. It checked the same 32 profiles, committed
rename, restart and replay, and matched running executable hashes. Its
[cleanup receipt](../evals/tantivy-dev-20261028/qualification-pilot-b21a25b813d0.json)
confirms no active trial machines or billable trial volumes; Fly retains deleted
volume records for provider-managed purge. This establishes the cloud lifecycle,
not full-corpus speed, disk fit or source freshness.

The [parallel dev health readback](../evals/tantivy-dev-20261028/post-pilot-live-health.json)
remained ready; production routing stayed on the native slot. The deployed dev
still uses the earlier source-v2 binary. The expiry-v4 candidate has not replaced it.

Full-corpus matched performance, complete expiry coverage, bounded reconciliation
rounds and engine-aware refresh ownership remain
open gates. The current MPS repository controller rejects a Tantivy route before
mutating slots, but that also prevents its native rollback command; an origin
allowlist change alone would leave installation, qualification and retention
assuming FLATPX05. Prepare and test those adapters before a later promotion.
No cutover readiness or production promotion is claimed.

The [full-corpus trial](../evals/tantivy-dev-20261028/qualification-full-4d62ead649f7.json)
stopped before comparison. Native preparation exited; its exact exception was
not retained. The harness contained a fixed 120-second full-prefix admission
bound and a writer that unnecessarily revalidated admitted input on startup.
Tantivy committed 2.3 million actors before cancellation; a bounded sample
reported 71.7% CPU steal with over 600 MB available memory. This is consistent
with Fly shared-CPU throttling or contention, not proof of a memory shortage.
Trial machines and billable volumes were retired. Query latency remains unqualified.

The next trial separates temporary performance-CPU preparation from the final
shared-CPU serving budget. It must resume the same verified files after restart,
prove the final two-shared-CPU/1-GiB/20-GiB shape, and pass a tiny transition test
before another full run. Preparation resources are reported separately; none of
its timings qualify serving speed. The
[synthetic staged cloud pilot](../evals/tantivy-dev-20261028/synthetic-staged-pilot-ddb646846adf.json)
passed: both original 20-GiB volumes survived preparation on two performance
CPUs/4 GiB and restart on two shared CPUs/1 GiB. Independent provider and running
executable checks passed; 224 queries per arm, nine update batches, durable
replay and process restart completed. Tantivy had zero truth/transport failures;
native mismatches remain recorded. Cleanup retired all trial resources. The
128-actor fixture supplies lifecycle evidence only, not full-corpus performance.

The companion MPS branch `codex/tantivy-controller-compatibility` includes
`ec61dbd` and `fba9ce8`: strict engine identity, a persistent native rollback
anchor with conditional compensation, immutable source-artifact retention and
checksum-pinned reuse of Typeahead's installer. Its actual HTTP/filesystem and
tiny Rust freeze/install/restart checks passed; it is not deployed. The refresh
fence remains until the complete cloud lifecycle is qualified.

The [staged full run `285b6147c50e`](../evals/tantivy-dev-20261028/qualification-full-285b6147c50e.json)
completed both preparations. Tantivy exited successfully at 22:47 UTC, but the
operator failed to advance that checkpoint into measurement before the 23:24 UTC
lease deadline. The independent reaper then removed every trial machine and
retired both volumes. No snapshots were available. This is an orchestration
failure, not an engine failure or performance result. Another full import must
wait for an independently running phase supervisor and a small real lifecycle
check that proves automatic advancement, result capture and cleanup.

Typeahead commits `a9745b4` and `02cda68` add source-linked portable bundles,
publication through the existing cloud-owned artifact publisher, and contracts
for two independently addressed Tantivy slots. The native source manifest is
verified by exact bytes before the immutable Tantivy manifest is published;
this path cannot change a mutable production pointer. Real local Rust and
rclone retry/corruption tests passed. Slot creation/deployment and complete live
refresh remain unqualified. The README remains an accurate entry point; these
branch-only contracts belong in the linked adapter documentation.

The [bounded Fly source sample](../evals/tantivy-dev-20261028/source-policy-wire-comparison.json)
measured the actual combined profile/policy response at 2,233,300 bytes for
4,000 profiles, versus 1,285,283 for a tuple projection prototype (42.45% fewer
response bytes). The earlier plain-current-state sample is not the combined
policy path and cannot establish its quota requirement. The prototype precedes
the final score-preserving encoding; sequential timings have uncontrolled cache
state and do not prove a speedup. The default two pages per minute remains far
too slow for daily full reconciliation. Wire size, writer progress and completed
round duration must all be verified on the actual candidate.

The branch's dev supervisor now requests at most 64 anti-entropy pages and
20 seconds of sweep work per cycle, following at most 30 seconds of discovery
and a 40-second polling interval. The 512-MiB hourly source allowance is
unchanged. Early reconciler exits wait out the original hour before another
process can receive a fresh allowance; a real early-exit child and a live/dead
server check verified that wait behavior locally. These are candidate settings,
not a measured coverage guarantee, and they are not deployed. Complete daily
rounds and concurrent-search behavior still require actual final-budget evidence.

The [operator-state audit](../evals/tantivy-dev-20261028/native-operator-inventory.json)
found source-one prefix writes only in tests across 13 revisions, with the flag
explicitly documented as future work. That is not a demonstrated shipped-feature
blocker. Production prefix-row absence remains unproven; no additional serving
scan was performed.

## Acceptance gates

- Preserve matching, ranking, exact-handle priority, canonical normalization,
  moderation, identity exclusions, hard PDS filtering and contextual preferences.
  Retain independent oracle cases for known native misses rather than demand
  parity with erroneous native results.
- Resolve source ordering and recovery before performance qualification. Timestamp
  seconds are not row revisions: activity updates can omit `updated_at`, and
  same-second moderation/restoration changes can conflict. The long-lived dev
  check found replay stalled and readiness false; do not override freshness.
- Use a private authoritative-observation protocol with document hashes, cursor
  CAS, monotone observation revisions and durable retry receipts. Keep generic
  timestamp replay strict and reject it after migration. Bound anti-entropy walks
  separately from timestamp polling; expose completed-round coverage honestly.
- Test actual commits, restart, crash before/after commit, lost acknowledgements,
  deletes/restores, moderation transitions, backdated records and source outages.
- Match corpus, query and update-feed hashes across isolated native/Tantivy
  controls. Serve on two shared CPUs and 1 GiB each, with separate load generation.
  Account for queue-inclusive tails, failures, writer progress, cache state,
  scheduling differences, merge pressure and recovery. Do not load-test production.
- Require no material speed regression in representative first-touch, warm and
  mixed workloads; retain mixed p95 <=50 ms/p99 <=100 ms targets. Diagnose the
  earlier diverse-prefix p99 of 390 ms instead of silently excluding that phase.
- Qualify a 20 GiB final serving disk budget, matching native allocation. Existing
  dev has a 40 GiB bootstrap disk; logical index savings alone do not qualify a
  smaller provisioned volume. Temporary preparation capacity must be reported.
- Make Worker forwarding/cache/error semantics and engine-aware transport/install
  contracts explicit. Preserve the exact production UI; the dev form must not
  initiate production writes. Public aliases remain equivalent.

## Small experiment sequence

Start with real local fixture tests and a bounded operational pilot before full
corpus trials. `deploy/tantivy-qualification` owns matched manifests and comparison.
The proposed full serving measurement is at most 15 minutes per arm, with a
separate preparation deadline, private independent disks and a cloud cleanup
owner. No new corpus-scale write begins before its storage budget, scratch root,
supervision and teardown are recorded. Only code and bounded receipts cross the
operator connection; source corpus stays cloud-to-cloud.

## Review and cutover planning

Commit scoped changes on this branch, preserving unrelated local work. Keep
current deployment state distinct from branch-only changes and dated receipts.
Once the gates pass, prepare conditional routing, cache propagation, request
draining, ingestion ownership, health checks and rollback steps with downtime
expectations. Discuss that concrete plan with Nate before changing production.

# Performance goal completion audit

Status: incomplete. Native qualification and guarded publication are verified;
the conditional routing candidate failed its fixed public qualification window
and restored the retained version. The next proof-reuse candidate is under guarded qualification.
Do not infer completion from smoke passing or from one earlier low-latency bucket.

| Requirement | Evidence and scope | State |
| --- | --- | --- |
| Existing two shared CPU / 1 GiB configuration | `release/final-machines.json` records both managed Machines, separate volumes, unchanged serving and rollback instance IDs, and their exact image digests. Native source 62266a3 is the qualified image. | Verified at the dated readback; recheck after qualification |
| Warm mixed search/sync p95 <=50 ms, p99 <=100 ms | Full-data independent Fly run `frozen-fly/20261027-45b615c88376/results.tgz`, candidate mixed: 2,016 attempts, p95 21.344 ms, p99 25.336 ms, zero failures, 40 real projection batches. Maximum was 548.85 ms; the percentile is not a maximum guarantee. | Verified |
| Varied-prefix cold behavior | Same run and input identity, candidate first touch: 1,008 attempts, p95 28.195 ms, p99 33.510 ms, zero failures. Baseline p99 4,353.03 ms and four failures remain in the archive. | Verified for the captured full snapshot and actual overlay |
| Ranking, moderation, scope, compatibility and aliases | 5,040 candidate status/body/scope-acknowledgment comparisons match the frozen reference. Intentional error cases remain visible. `release/checks/smoke.log`, `release/checks/ranking.log` and the conditional candidate's real workerd/R2/HTTP tests cover release semantics. Canary smoke passes at each percentage. | Verified for tested cases; conditional candidate rejected |
| Failure-inclusive accounting and bounded saturation | Client queue time, timeouts, incorrect bodies and unexpected statuses are retained. `linux-fixtures-20261027-458502984299/test.log` records seven real native fixture tests, including 160 concurrent client requests with live projection and seven occupied background slots. Rejected cold/read-ahead runs remain recorded. | Verified; no claim of arbitrary future capacity |
| Freshness and live sync | `release/final-health.json` records a healthy public probe with 58-second lag. `release/final-sync-health.json` records seven successful incremental sync spans and 74 routed health spans for 18:10–18:45 UTC, no error-level spans for those operations. The canary checks lag below 600 seconds and the expected build throughout. | Verified at dated readbacks; final check pending |
| Substantial public reduction toward ~169 ms | Retained release: first post-release window 197.46 ms; later clean window 163.47 ms; confirmation 197.93 ms. Full 17:55–18:50 interval: 809 misses, mean 185.92 ms, 836 reconciled HTTP-200 answers. The low window did not persist. Conditional candidate failed its 19:10–19:25 UTC window at 200.58 ms. The next candidate requires >=100 misses, full count reconciliation, no unexpected HTTP statuses and mean <=169 ms. | Not yet verified |
| Network-comparable interpretation | Both versions retain IAD placement, native backend, one-second route TTL and public response-cache policy. Paired diagnostic requests use the same queries and ATL/IAD path. Natural public windows have different request/client mixes and are observational; neither their percentages nor provider-wide counts prove a randomized causal gain. | Boundaries documented; final paired readback pending |
| Guarded rollout and retained rollback | Native publication used inactive independent storage and completed conditional promotion through flow bbcfa61e. Conditional routing source passes Worker tests, TypeScript, real runtime tests and CI. The bounded job smoke-gates 10%/50%/100%, checks ownership and restores 40a014e4 on failure/interruption. | Publication verified; conditional canary restored retained transport |
| No production load test, resize or restart for experiments | Full-corpus trials used independent inactive-slot forks and were retired. Current work changes only the transport version; serving/rollback images, volumes, instance IDs and routing object are untouched. Canary traffic is ordinary traffic plus correctness/health checks. | Verified within recorded actions |
| Cleanup | Full-data payloads and private Fly Machines are retired; named trial credentials are revoked in `release/temporary-resource-audit.json`. The small R2 profiling Worker is deleted. The bounded native tail's leftover lease was removed with proven ownership in `route-read/tail-retirement.json`. | Conditional canary terminal; its tail lease retired, no continuing observer |
| Documentation and public claims | README and operations distinguish qualified native behavior, dated observations and the unqualified conditional candidate. The public explanation contains a dated snapshot and sanitized measurements, without infrastructure identifiers or raw traffic-source lists. | Final result and publication status update pending |

Provider analytics for 17:55–18:50 UTC record 4,294 successful public Worker
invocations, 131 client disconnects and 367 successful transport invocations,
with zero reported runtime errors. These adaptive, provider-wide counts have a
different scope from endpoint counters and are not a counter reconciliation or
a claim that every visitor received an answer. The cancellations remain explicit.

The conditional canary is terminal (`restored-retained`), with mean 200.58 ms
and no HTTP errors. It does not complete the public requirement. Post-rollback
smoke passed. There are no continuing canary observers from that run.

At 20:23 UTC both managed Fly instance IDs and final resources were verified
unchanged (`route-proof/canary/machine-guard.json`). Public query-free timings
are live after full smoke. The proof-reuse candidate passed its 10% gate; a
control-plane 10500 error prevented progression and safely restored retained
transport. The single retry is now at 50%, with fixed natural-traffic gates.
This remains incomplete until that job has a terminal qualified result and all
final correctness/freshness/counter and cleanup readbacks are checked.

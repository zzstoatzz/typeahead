# Tantivy cutover readiness

Owner: `codex/tantivy-cutover-readiness`. Started October 29, 2026.
Production routing, serving/rollback machines and live ingester remain protected.
The dev API and production-identical frontend remain parallel to production.
This task prepares evidence and a later cutover proposal; it does not authorize promotion.

## Current deployed status — October 30, 2026

The checkpoint publisher completed build `b1793346536-cp-7c8322df79` and
published its immutable 4,712,079,360-byte bundle. Managed slot A installed it
on two shared CPUs, 1 GiB RAM and a 20-GiB disk; public health is ready and
ordinary source polling advances. The
[live-source audit](../evals/tantivy-dev-20261028/checkpoint-live-source-audit-first.json)
passed 51 requests and five bounded source reads: eight recent actors matched
canonical identity, hydration and PDS inclusion/exclusion. This sample did not
supply moderation or known-old-handle deletion witnesses and does not claim
causal mutation verification. A separate bounded
[deleted-document hash check](../evals/tantivy-dev-20261028/checkpoint-live-deleted-document-hash.json)
proved all eight source-confirmed tombstones are stored as canonical deleted
documents. Existing moderation overrides supplied no live witness.

The preparation controller stopped on a startup HTTP 503 after installation.
MPS `ffe86a0958b4b5a7f3125452e3fe0ded748b4322` fixes transient urllib startup
retries within the existing deadline. The same operation completed against
the installed volume, including cached restart and requalification. Its
[prepared plan](../evals/tantivy-dev-20261028/slot-a-prepared-plan.json) records the
exact admission and provider binding. The temporary controller VM and deployment
were retired. The production-identical dev preview now points at A. Its
[public smoke](../evals/tantivy-dev-20261028/checkpoint-public-smoke.json) passed
53 requests with no failures or inconclusive checks. The
[real-origin rollback rehearsal](../evals/tantivy-dev-20261028/checkpoint-live-route-rehearsal.json)
passed native → A → native with 13 bounded GETs, both aliases and stale-ETag
rejection. Its routing object was isolated; production routing was not changed.
Production remains native. Historical attempts below are evidence, not current
instructions to repeat their builds or trials.

## Readiness requirements

| Requirement | Current evidence | Remaining limit |
| --- | --- | --- |
| Complete matching and PDS semantics | Full-corpus functional oracle and 53 deployed preview requests pass | Source PDS metadata remains incomplete; missing hosts cannot be inferred by either engine |
| Serving speed and resources | Matched 5,224-request trials beat native; [live provider readback](../evals/tantivy-dev-20261028/checkpoint-final-provider.json) confirms 2 shared CPUs / 1 GiB / 20 GiB | No production traffic replay or RAM-reduction claim |
| Source freshness and durable deletion | Live source audit, eight canonical deleted-document hashes, cached restart and [freshness readback](../evals/tantivy-dev-20261028/checkpoint-final-freshness.json) pass | Public disappearance of these specific old handles was not observed |
| Moderation | Same-D1 controlled hide/unhide/expiry tests and complete fresh canonical policy | No existing operator hide override supplied a live sampled witness |
| Native rollback | Real-origin isolated Worker rehearsal passes; native remains independently healthy | Production cache propagation duration is not measured |
| Publisher and installation | Actual cloud checkpoint publication, immutable manifest/bundle, A install and cached restart pass | Recurring two-slot retirement/refresh is not yet qualified on live slots |
| Review and rollout | [Typeahead draft PR](https://github.com/zzstoatzz/typeahead/pull/1) and [controller draft PR](https://github.com/zzstoatzz/my-prefect-server/pull/5) | Release proof and exact approved rollout commands remain to be finalized after open gates |

The candidate's typed checkpoint admission is valid and fresh. Its background
full-source reconciliation is still on its first round; ordinary polling is not
being represented as completed full coverage. The expiry supervisor remains
active. Neither a positive final release proof nor production promotion is claimed.

## Qualification evidence

The [matched full-corpus component](../evals/tantivy-dev-20261028/full-corpus-performance.md)
passed on the required two-shared-CPU/1-GiB/20-GiB serving shape. Shared-feature
mixed p95 was 8.20 ms for Tantivy versus 37.28 ms native; p99 was 9.95 versus
41.95 ms. Tantivy had zero oracle/transport failures across 5,224 requests,
nine durable update stages, restart and the additional functional witnesses.
Cleanup completed and a fresh provider read showed zero trial machines.
This clears the frozen-feed performance component, not live-source or slot release
qualification. Candidate cloud publication uses the existing publisher with
immutable source/twin artifacts and no channel/routing updates; its
[run receipt](../evals/tantivy-dev-20261028/candidate-cloud-publication.json)
records exact pins and bounds. The attempt failed before tasks: the worker-poll
response omitted per-run job variables, so the executable and pulled source
remained on the old deployment pin. The dedicated metadata-only probe reproduced
that omission and was cancelled without creating a VM. The failed builder VM is
confirmed deleted. The seven-to-eight-hour row-walk bootstrap is abandoned at
Nate's direction; do not retry that launch recipe. The next bootstrap must reuse
a consistent bulk SQLite checkpoint, prove its freshness and full policy coverage
without inventing a completed reconciliation round, and pass a minutes-scale pilot
before any full run. The bounded SQLite/WAL fixtures now preserve full
actor data and policy; a 100,000-document synthetic host import took 4.509 seconds,
which is not a production estimate. The existing reader retains typed checkpoint
provenance and a checkpoint-only walk can compare unchanged documents without
reindexing. The [provider committed-prefix probe](../evals/tantivy-dev-20261028/wal-committed-prefix-probe.json)
observed a short successful batch ending at committed frame 71 in 1.023 seconds.
Only bounded source/WAL metadata returned from the dev guest; no corpus crossed
the operator connection. This establishes an observed finite WAL prefix, not that
a complete exported database has been replayed or live coverage qualified.


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

The [October 30 deployed-preview HTTP smoke](../evals/tantivy-dev-20261028/current-preview-http-smoke.json)
returned 200 for both public aliases, a hard PDS filter and a contextual
preference request. The aliases were byte-identical and the hard filter was
acknowledged, but the earlier backend did not acknowledge the preference.
This smoke therefore failed its preference gate. An empty filtered result
does not establish filter recall, and these four sequential requests are not
a performance benchmark. The candidate must pass these semantics on its actual
deployed public path before the dev site can supply release evidence. The
[deployed preview guard](../../deploy/tantivy-dev-ui/scope-verification.json) now
rejects those unsupported preferences with 503 using the shared public scope
contract; malformed scope returns 400. Both aliases and byte-identical production
HTML remain verified. This removes silent fallback, not the backend capability gap.

Live-source completed-round coverage and actual cloud slot/refresh qualification
remain open gates; the frozen-feed performance component is now proven above. Branch-only code now includes
engine-aware publication, installation, retention and native rollback; its local
checks are distinct from deployment or release qualification. The deployed dev
and production services have not been cut over to this branch. No cutover
readiness or production promotion is claimed.

The [full-corpus trial](../evals/tantivy-dev-20261028/qualification-full-4d62ead649f7.json)
stopped before comparison. Native preparation exited; its exact exception was
not retained. The harness contained a fixed 120-second full-prefix admission
bound and a writer that unnecessarily revalidated admitted input on startup.
Tantivy committed 2.3 million actors before cancellation; a bounded sample
reported 71.7% CPU steal with over 600 MB available memory. This is consistent
with Fly shared-CPU throttling or contention, not proof of a memory shortage.
Trial machines and billable volumes were retired. That trial supplied no query latency result.

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

The automatic supervisor subsequently passed two real 128-actor Fly pilots:
[`b34d01d0cf2f`](../evals/tantivy-dev-20261028/supervised-pilot-b34d01d0cf2f.json)
proved phase advancement, result capture and cleanup;
[`08552ca2adfa`](../evals/tantivy-dev-20261028/supervised-pilot-08552ca2adfa.json)
also uploaded and ran the SHA-bound post-measurement probe. Its 25 queries,
nine update stages, three hydration witnesses and hide/delete/PDS-change checks
passed with actual provider/runtime evidence; native remained unchanged and
cleanup completed. Neither synthetic pilot qualifies full-corpus performance.

Full retry `672c3d51434a` also ended before measurement: an observation/guard
error reached an unconditional cleanup path. The
[terminal reaper proof](../evals/tantivy-dev-20261028/full-672c-reaper-terminal-proof.json)
shows that its retained identity/runtime shape still validates and its stop was
requested by controller cleanup. The original underlying error subtype was lost;
no provider-status or engine-failure conclusion is supported. All resources were
retired. The corrected supervisor retains work on uncertain reads, retries the
same IDs/intents, preserves failed results for diagnosis until the original lease,
and retries cleanup separately. Forty-three local lifecycle HTTP tests passed.
The [real Fly observation-fault pilot](../evals/tantivy-dev-20261028/observation-fault-pilot-b736d9888c4a.json)
then retained resources through one deliberately injected local client read
failure, resumed without recreating them, completed the functional probe and
cleaned up. Provider history confirms no controller destruction before result
capture. The injection was local; this is not a claim of a real Fly outage.

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
The [final score-preserving SQL sample](../evals/tantivy-dev-20261028/source-policy-final-wire-sample.json)
then measured 1,310,704 profile/policy bytes for 4,000 profiles, plus 244,804
identity-walk bytes. The final profile response is about 41% smaller than the
earlier object projection. This remains a bounded read-only sample, not a
completed-round or concurrent-search result.

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

## Prepared operator inputs

The [cutover proposal](../evals/tantivy-dev-20261028/cutover-proposal.json) contains
the exact candidate, native anchor, plan SHA and expected route ETag, plus the
promotion and rollback parameter shapes. It is deliberately not executable until
the release receipt is complete and Nate authorizes the production change.
A changed route/ETag requires a freshly prepared plan; do not substitute a current
ETag into an older plan.

After approval and after landing the reviewed controller/Worker source on main,
use the existing cloud deployment with its reviewed immutable job pins. The
initial promotion is an explicit `tantivy_operation=promote`, never an automatic
refresh. The rollback invocation is:

```sh
just prefect --no-prompt deployment run typeahead-index/typeahead-index -p rollback=true
```

This restores the independent native anchor through conditional routing; it does
not stop, rebuild or resize a serving machine. The controller must be running the
reviewed engine-aware revision before either command is used. Promotion must use
the complete `promotion_parameters` from the proposal after its release-proof
fields are filled and revalidated; null fields are not approved placeholders.

The compatible Worker and transport are deployed while native is still selected.
Their isolated real-origin rehearsal passed both aliases and native → candidate →
native recovery. Both backends remain running during the eventual conditional
route change. Existing cached responses may temporarily retain the previous
backend; the response-cache TTL is 60 seconds and the controller permits 120
seconds for its public identity/canary verification. These configuration values
are not a measured global propagation guarantee. Do not promise an outage or a
zero-downtime bound: no backend shutdown or DNS transition is planned, and failed
verification conditionally restores the previous routing record.

## Review and cutover planning

Commit scoped changes on this branch, preserving unrelated local work. Keep
current deployment state distinct from branch-only changes and dated receipts.
The following procedure is prepared for later approval; its open gates are not
waived by this document. No production cutover is authorized.

1. Finish the matched full-corpus comparison, post-measurement functional
   witnesses, live-source completed-round test, final-budget resource checks,
   slot restart/refresh recovery and production-identical dev UI/API smoke.
   Bind their actual receipts to the exact proposed slot image, executable,
   strategy, source generation and deployment manifest. The frozen benchmark
   image supplies engine-component evidence; it cannot by itself qualify a
   different serving image. Publish no positive release proof while any gate
   remains missing or inconclusive.
2. Rehearse the complete controlled route change and compensation against an
   isolated routing record and the same Worker/transport code. Exercise cached
   and uncached requests, both public search aliases, hard PDS filtering and
   contextual preference. Record failures, backend identity, source lag and
   latency through the change. Establish the cache barrier and measured
   propagation interval from that rehearsal before giving a downtime estimate. The
   [local workerd/R2 rehearsal](../evals/tantivy-dev-20261028/route-rehearsal-local.json)
   passed promotion, compensation, scope acknowledgements, cache isolation and
   stale ETag/operator-change protection. Its approximately one-second local
   route-memory convergence is not production propagation evidence. The order
   is CAS, uncached identity convergence, then cache-generation rotation and
   both-alias miss/hit checks; old cache generations can retain old answers.
3. Present Nate the exact candidate plan, current production route/ETag,
   immutable image and artifact identities, empirical release proof, native
   rollback anchor and rollback command. Obtain explicit cutover approval.
   Production Worker/transport deployments and the routing-target change remain
   deferred until that approval.
4. Deploy the engine-aware Worker/transport compatibility code while the native
   route remains selected. Smoke the actual native public path before proceeding.
   Keep the existing relay ingester, Turso writer and cloud snapshot publisher
   ownership unchanged. Native search has its own background Turso projection;
   preserve and independently check that path so rollback stays fresh after
   request-indexing pushes stop reaching the native route.
5. Recheck the candidate's actual provider instance, final two-shared-CPU/1-GiB/
   20-GiB allocation, source/policy freshness, completed-round coverage and
   HTTP identity. Recheck native rollback health. Refresh the prepared plan if
   the expected route or ETag changed. A mutation lease can protect an inactive
   slot; it must not change the current target and must survive an intervening
   native rollback.
6. Establish a new `SEARCH_CACHE_GENERATION` barrier, or prove that the existing
   60-second response-cache TTL has drained, as rehearsed. Use the explicit
   guarded promotion operation with the exact approved plan SHA and ETag.
   Preserve the complete native anchor and previous target in the conditional
   routing update. DNS, public URLs and frontend markup stay the same; the
   already-running candidate takes new backend requests without stopping the
   native process. Check uncached canonical/alias/filter/preference responses
   immediately, then cached responses after the measured propagation barrier.
7. If verification fails, compensate only while the failed candidate record and
   its ETag are still current; restore the exact prior envelope and its native
   anchor. An operator's newer routing change wins. Re-establish the cache
   barrier, verify actual native identity/freshness, and retain the failed slot
   for bounded diagnosis. Never rebuild or resize the native anchor as part of
   rollback. Resume future automatic Tantivy refresh only under the separately
   qualified two-slot retirement/lease/retention policy.

The intended change has no planned backend stop or DNS transition. A zero-downtime
claim and a numeric propagation bound remain unproven until the isolated public
rehearsal passes. Worker deployment and R2 routing are separate updates; the
procedure and compensation must account for that explicitly.

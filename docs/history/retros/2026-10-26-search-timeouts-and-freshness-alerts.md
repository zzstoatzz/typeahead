# Search timeouts and misleading freshness alerts — October 26, 2026

## What happened

Typeahead had a real serving-availability incident at approximately 20:00 Chicago
on October 26 (01:00 UTC, October 27). Eleven public search requests followed the
backend-failure path and still returned HTTP 200; that code path returns an empty
`actors` list. The freshness notification described the service as stale even
though its overlay sync continued successfully. A successful health check a few
seconds later did not establish that search had been unaffected.

The afternoon repair had restored missing telemetry, corrected the sync alert's
retired service target, and repaired a separate TLS failure. Those fixes held
through the evening event. They did not solve the serving tail-latency and HTTP
admission problems exposed here.

**RCA status:** request failures, error masking and notification behavior are
confirmed. Health starvation under connection saturation is reproduced in the
exact deployed image. The production timing strongly supports admission starvation,
but no production admission/queue trace establishes which connections occupied the
pool or why the longest search took 8.4 seconds. That part remains open; this is
not a claim that CPU, disk, SQLite or a cold page cache has been proven responsible.

## Impact and evidence limits

The fixed observation window is **00:59:40–01:02:00 UTC on October 27**, 140 seconds.

| Surface | Recorded result | Interpretation |
| --- | --- | --- |
| Transport `/search` | 12 HTTP 503 responses, 23 HTTP 200 responses | 34.3% of these 35 recorded search responses failed. This is not the error rate across all public requests or all attempted origin calls. |
| Public search Worker | 11 distinct `search_backend_unreachable` request IDs, correlated with 11 outer HTTP 200 responses | Failures were hidden from status-only accounting. The matching source branch returns `{actors: []}` without caching it. Response bodies were not captured in the logs. |
| Transport `/health` | One 503 and three 200 responses | One observed availability failure; it does not establish stale data. |
| Transport `/index/status` | One 503 and three 200 responses | Snapshot identity was temporarily unobservable, not proven absent or old. |
| Overlay sync | Successful 26.29-second pass: 461 updates, 107 clearances, 44 deletions | No sync stoppage in the inspected interval. These are normal mutations, not lost records. |

The 12 transport search failures completed between **01:00:54.869 and
01:01:03.970 UTC**. That is an observed failure interval, not a continuously
measured outage duration. The public failure-log interval is
01:00:54.503–01:01:03.952. Independent layers use different request IDs; these
counts are not a complete request-by-request mapping between layers.

The records do not establish unique affected users, the number of suggestions
that should have been returned, or downstream client behavior. Cached responses
are outside the transport denominator. Native `search` spans are sampled at 1/10;
we do not derive a population latency percentile from them. The transport count
receipts report sampling interval 1. Search terms, client IPs and credentials are
excluded from the retained evidence.

## Timeline

All times below are UTC. Evening times fall on October 27 UTC and October 26
in Chicago, five hours behind UTC.

| Time | Event | Evidence |
| --- | --- | --- |
| October 25 | Production moved between independently prepared smaller backends; superseded backends were later retired. | [Refresh and retirement record](../ops/automatic-refresh-20261025.md). |
| October 26, before afternoon repair | Both managed apps lacked a Logfire write token; sync alert still selected retired `typeahead-search-compact`. Persistent Turso TLS failures were later observed. | [Monitoring repair](../ops/search-monitoring-20261026.md). Exact telemetry-gap start and TLS failure onset are not established here. |
| October 26, 18:35:26 | Controlled image update completed; serving readiness returned with the same disk and snapshot. | [Repair receipt](../evals/search-monitoring-20261026.json). This involved a serving-process restart. |
| October 26, 18:40:20 | Recovered sync pass completed; public freshness subsequently reported 200 with 20 seconds lag. | Same receipt. |
| October 26, 18:40:45 | Scheduled sync-alert evaluation returned no findings or query errors. | Same receipt. |
| October 27, 01:00:23.165 | Routed native health handler reported ready, lag 279 seconds; handler duration 0.47 ms. | [Native spans](../evals/rca-20261026/native-spans.json). |
| 01:00:44.387 | Incremental sync began. | Same native spans. |
| 01:00:54.503 | First retained public backend-timeout log in the observation window. | [Public failure logs](../evals/rca-20261026/public-search-failures.json). |
| 01:00:54.869 | First retained transport search 503 in the window. | [Transport search failures](../evals/rca-20261026/search-failures.json). |
| 01:00:56.536 | Transport health request returned 503 after 3,211 ms. | [Health transport events](../evals/rca-20261026/health-transport.json). |
| 01:00:59.750 | Cron recorded `freshness_stale`, reason `search service /health unreachable`. | [Freshness events](../evals/rca-20261026/freshness-notification.json). |
| 01:01:00.007 | Discord webhook returned 204 for that state transition. | Same freshness events. |
| 01:01:00.702644 | Native log recorded an 8,393 ms, one-character search completing. | [Slow-search logs](../evals/rca-20261026/slow-searches.json). |
| 01:01:00.702940 | Native health handling began about 0.30 ms after that log; handler took 11.58 ms and reported lag 316 seconds. | Native spans. The adjacency supports, but does not by itself prove, an accept-loop stall. |
| 01:01:02.956 | Next recorded transport health request returned 200 after 2,672 ms. | Health transport events. |
| 01:01:03.970 | Last retained transport search 503 in the window. | Transport search failures. Health recovery did not mean all searches had already recovered. |
| 01:01:10.676 | Incremental sync completed with status OK. | Native spans. |
| 01:01:22.956 | Following transport health response returned 200 in 137 ms. | Health transport events. |
| After investigation | Live public freshness returned 200 with 19 seconds lag; no serving restart, resize, route change or threshold change was performed for the evening event. | [Dated investigation](../ops/search-monitoring-20261026.md#2000-chicago-freshness-alert). |

## Root causes

### 1. Connection admission can block the accepting thread

The deployed server creates a threaded I/O runtime with `async_limit=8`
([main.zig](../../services/src/main.zig)). Each accepted connection is handed to
`tasks.async`. Once the asynchronous budget is exhausted, the task can run inline
on the accepting thread. The connection handler may then wait for headers, execute
a slow search, or otherwise block admission of subsequent connections.

This is not a reservation of eight HTTP workers: Zig 0.17 counts `concurrent`
background tasks and `async` handlers in the same `busy_count`, so sync, watchdog
and promote work also consume the budget. This budget applies to **connections**,
not just actively executing searches.
`handleConnection` loops over keep-alive requests; idle/header-reading connections
can occupy slots too. Its special handling disables keep-alive for an inline
connection, but only after receiving headers, and it does not bound search work.
Calling this fallback “graceful degradation” in the source did not establish that
health traffic remained available.

Health, readiness, search and index-status requests share that listener and
admission path. A health handler that takes less than a millisecond after it starts
can still miss a three-second external deadline while waiting to be admitted.
The native health span begins inside the handler, so it omits that wait.

The local [reproduction](../../scripts/rca/health_queue.py) runs the exact deployed
Linux image without credentials or a corpus. Nine held partial-header connections
ensure the asynchronous budget is exceeded and an inline connection blocks
admission. A subsequent real HTTP health request exceeds a three-second deadline,
then succeeds after those connections are released.
See the [reproduction receipt](../evals/rca-20261026/health-queue.json).

This proves a failure mechanism in the deployed admission design. It does not
prove partial-header clients were the production trigger, or that there were eight
simultaneous expensive searches. The incident's multi-second search completions
and immediately following short health handler strongly support the same class
of admission delay. Direct queue/inline instrumentation is still needed to close
that attribution.

### 2. Origin deadlines expired while useful work was still executing

The transport and public search hop impose three-second deadlines. Observed
native searches took 3.69, 4.26 and 8.39 seconds in the affected interval. A small
successful probe set could not establish that those deadlines were safe for the
full workload. A bounded hydration candidate count also does not bound physical
I/O, queue time or every overlay operation.

The smaller serving machine had two shared CPUs and 1 GiB RAM. Reduced headroom
is a contributing risk, not a demonstrated physical root cause. There is no
retained CPU-steal, throttling, page-fault, storage-read or per-phase database
capture for the failed requests. Calling every slow query “cold” would overstate
the evidence. Sync overlapped the event; overlap alone does not prove contention.

### 3. Public failure handling reported success with an empty result

For an unscoped request, with unreachable fallback disabled, the public Worker
catches backend errors, logs `search_backend_unreachable`, and returns
`json({actors: []})` ([search.ts](../../src/handlers/search.ts)). The response is
not cached, but it has status 200. A client cannot distinguish an unavailable
engine from a genuine zero-match search using that response.

All 11 retained failure-log request IDs correlate with outer HTTP 200 events.
The low apparent 5xx count therefore understated user-visible failure. The earlier
change making transport failures return 503 improved the inner layer without
fixing this public failure contract. PDS-scoped failures already use explicit
unavailability, so the two public paths have inconsistent failure semantics.

### 4. The freshness alert collapsed unavailable and stale into one state

`measureFreshness` treats an unavailable health response as `ok=false`, then sets
`stale=true`. It also leaves lag, snapshot identity and search-probe state unknown.
`notifyFreshnessChange` labels the message “serving is stale” and renders a null
probe state as `FAILING` ([freshness.ts](../../src/handlers/freshness.ts)).

The alert correctly exposed failed serving access. Its wording asserted more about
data freshness and the probe result than the measurement established. This was
not merely a cosmetic false positive: search requests were failing at the same time.

The notifier samples state in the hourly Worker cron. Reading the public endpoint
does not update its saved notification state. A brief failure can therefore leave
the last Discord message stale until the next hourly observation. The nine o'clock
Chicago run was the next opportunity, not a verified recovery notification at the
time of this report.

### 5. Release acceptance and migration checks covered narrower properties

The October 25 [cutover probes](../ops/search-cutover-result-20261025.md) passed
five rounds over roughly two minutes, including one advancing sync cycle. The
[refresh record](../ops/automatic-refresh-20261025.md) separately documents two
cold-query checks exceeding the unchanged 2.5-second budget before a retry passed.
Those checks established correctness and recoverability, not sustained concurrent
availability of a small serving machine.

We retained a known tail-latency limitation without a demonstrated health-admission
isolation gate. Startup and storage improvements made restarts cheaper; they did
not prove all requests would meet the origin deadline. The October 19 telemetry
repair also did not become a mandatory property of the later replacement-app
installer until October 26, allowing the missing-token failure to recur during
migration. Alert identity and telemetry credentials were not initially carried
as part of the replacement backend's complete serving contract.

This was also not a newly discovered scheduler hazard. Commit `38e0a27` on
October 9 closed inline keep-alive responses after earlier starvation; its
[concurrency tests](../../scripts/snapshot_compaction/test_release_concurrency.py)
exercise 8 and 32 well-formed persistent clients. That mitigation does not bound
header receipt or one expensive request, and those tests do not inject either
condition. The response needed to check that earlier fix before assuming the
remaining admission path was isolated.

## What went wrong in the response

- I initially summarized the evening event as a brief health timeout before
  quantifying search impact. That understated eleven public requests following the
  failure path. A recovered health check was not enough to close impact analysis.
- I called accept-loop starvation a likely explanation without first reproducing
  the deployed admission behavior. The exact-image reproduction now supports the
  mechanism; the production queue attribution remains qualified.
- I wrote an operations note and stopped. [This repository already requires a
  retro after an incident](README.md#when-to-write-one). Nate had to request it.
- The afternoon repair was complete for its narrow monitoring scope, but the
  closeout did not make the still-open serving reliability risk prominent enough.
  “Telemetry restored” and “safe tail latency under concurrent traffic” needed
  separate acceptance statements.
- I did not inspect the outer Worker failure contract during the first diagnosis.
  It converts the transport's correctly classified failure into a successful empty
  response, so checking HTTP status alone missed actual impact.

What worked: the restored native telemetry and unchanged source/image identities
made this investigation possible; the independent freshness probe detected a real
availability failure; sync checkpoints continued advancing; the transport exposed
503s; read-only historical queries and an isolated container reproduction avoided
another production experiment. There was no need to restart or resize the live
service to obtain this evidence.

## Action items

Owners below name the responsible component/workstream, not an assigned human.
Open items are required follow-up work; none are claimed deployed by this retro.

- [ ] **P1 · Search HTTP admission:** Prevent unbounded/slow connection work from running on the accepting thread; provide bounded admission and health capacity that survives saturation. Verify with real TCP tests covering active searches, partial headers and idle keep-alive clients, including the current reproduction. Do not replace this with an unbounded thread-per-connection design.

- [ ] **P1 · Public search API:** Make engine unavailability distinguishable from genuine zero matches for unscoped requests. Verify actual outer status/body, PDS behavior, alias equality, non-caching of failures and downstream client compatibility; update all public API documentation with the chosen contract.

- [ ] **P1 · Native observability:** Record accept-to-handler wait, active/inline connection counts and bounded search phase timings. Verify that queue delay is visible even when the external caller times out before a search/health span starts.

- [ ] **P1 · Qualification/release:** On an isolated final-size backend, qualify mixed health/search/sync work with equivalent cold and warm states, a fixed request set, concurrency and explicit timeout/error acceptance criteria. Record rejected runs; a later retry must not erase an intermittent failure from the acceptance record. No production load test or corpus download is authorized by this action item.

- [ ] **P2 · Freshness monitor:** Represent unavailable, stale, not-ready and healthy results explicitly; preserve an unknown probe result as unknown. Verify a failed health request with fresh data, a true lag breach, search-only failure, missing index metadata and recovery through real HTTP.

- [ ] **P2 · Alert recovery:** Specify and verify a bounded recovery/reconfirmation path so one hourly sample cannot leave a misleading unresolved notification for nearly an hour. Keep persistent failure detection; cadence changes and outbound test messages need their normal authorization.

- [ ] **P2 · Performance diagnosis:** After queue instrumentation, determine the physical cause of long requests using isolated comparable data and CPU/throttle, I/O/page-fault and database phase evidence. Do not infer that downsizing, cold cache or sync contention is proven by timing correlation alone.

- [x] **P2 · Release observability:** Require a telemetry credential on managed installers; provision both slots; select the routed production service in the sync alert; verify real health/sync spans and scheduled alert evaluation. Shipped earlier October 26, not a fix for admission starvation.

- [x] **P2 · Incident response:** Preserve impact counts, correlate outer responses, reproduce the failure mechanism and publish this retro before calling the incident analysis complete.

## Reproduction and evidence

Run `python3 scripts/rca/health_queue.py` only with Docker and the pinned image
already present. It uses `--pull=never`, a read-only root, a 16 MiB temporary
filesystem, one CPU, 256 MiB RAM and at most nine held test connections plus the
probe. It has no credentials, corpus, production route or mounted persistent
volume. It removes its container and scratch storage on normal completion,
exceptions and SIGTERM. Force-killing the harness can bypass Python cleanup;
remove its uniquely named `typeahead-rca-*` container if that happens. This is a
mechanism test, not a full-corpus capacity or production latency benchmark.

The sanitized evidence directory is [rca-20261026](../evals/rca-20261026/):

- [Transport response counts](../evals/rca-20261026/transport-counts.json), filtered
  to root invocation events, grouped by trigger and response status.
- [Transport search failures](../evals/rca-20261026/search-failures.json).
- [Public failure logs](../evals/rca-20261026/public-search-failures.json) and
  [correlated outer responses](../evals/rca-20261026/public-response-correlation.json).
- [Health transport requests](../evals/rca-20261026/health-transport.json) and
  [notification events](../evals/rca-20261026/freshness-notification.json).
- [Native health/sync spans](../evals/rca-20261026/native-spans.json) and
  [slow-query completions](../evals/rca-20261026/slow-searches.json).
- [Exact-image queue reproduction](../evals/rca-20261026/health-queue.json).

Cloudflare receipts retain query IDs and the millisecond time window; native
receipts retain trace IDs and selected fields. The before/after sources and
[afternoon deployment receipt](../evals/search-monitoring-20261026.json) distinguish
what shipped from what this report only investigates. No serving behavior,
resource size, thresholds or schedules were changed while writing this RCA.

## Sizing follow-up — October 27, 2026

A later slow-request window at 05:04 UTC had retained Fly metrics: memory
pressure and disk waits rose while CPU throttling stayed zero. An inactive
1 GiB → 2 GiB → 1 GiB comparison reproduced extra repeat-query disk reads at
1 GiB with identical responses and unchanged sync watermarks. Two GiB eliminated
those repeat reads and improved warm tails. This establishes a memory-capacity
effect for that working set; it does not prove the physical cause of the earlier
incident or close the admission and API-error action items above. See the
[measurements](../evals/sync-hardening-release-20261026/memory-findings.md) and
[native release record](../ops/sync-hardening-release-20261026.md).

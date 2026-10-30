# Isolated Tantivy dev backend

Started October 28, 2026; operational status October 29 (Chicago). No production cutover.

The later cutover-readiness check found replay stopped on another source-version
conflict. A source-owned observation repair was deployed to this dev app and caught
up about 15,000 changes; [fresh checks](readiness-ui-verification.json) show readiness
restored. The new read-only UI still serves identical production homepage bytes.
See [the repair image](readiness-repair-image.json) and
[readiness plan](../../plans/tantivy-cutover-readiness.md). Policy-v3, performance
qualification and cutover adapters are branch work, not production changes.

The independent Fly app is `typeahead-tantivy-dev`, machine
`840e95a2153308`, with its own 40 GiB volume `vol_4oj6mqjpnlgeyqxr`.
The public API is https://typeahead-tantivy-dev.fly.dev and the development UI is
https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev. Both were deployed and healthy
at the initial qualification in the final two-shared-CPU, 1 GiB serving shape. The UI Worker serves the deployed
production homepage verbatim through a service binding and routes search to this
Tantivy backend. [HTTP verification](ui-verification.json) confirms identical
homepage bytes, Tantivy health/search and byte-identical API aliases; visual QA
was not performed. Production routing remains unchanged.

The deployed image is
`registry.fly.io/typeahead-tantivy-dev@sha256:5342a3b41539777658b2a5e8a64935593ee82a6609f4798e2f12da5bc91d48ec`.
The admitted index and committed source cursor survived the final-size restart.
Fresh replay readiness is enforced without stale/partial overrides.

The source is the current serving generation's immutable R2 release
`b1793109259-2b36-px05`, containing 12,816,605 actors. R2 download, validation,
streaming export, indexing, and subsequent fixed read-only Turso replay run on
the new Fly VM. No corpus passes through HeavyPad or the laptop. Serving and
rollback disks are not copied or used as scratch.

The cloud operational pilot passed all 12 backend smoke groups, five independent
adversarial gates, nine exporter tests, and ten replay tests. These exercise
synthetic fixtures and lifecycle failures; they do not establish full-corpus
latency or readiness. See [cloud-pilot.log](cloud-pilot.log),
[cloud-pilot.json](cloud-pilot.json), and [audit.md](audit.md).

The first full import rejected actor 11,284,974 because its PDS endpoint uses
localhost and an explicit port. That value is unrepresentable by the existing
DNS-host scope policy; it does not justify dropping the actor. The replacement
backend retains raw optional PDS data, leaves canonical scope empty, and counts
nonempty unscopable PDS separately from missing PDS. Query scope validation stays
strict. The first attempt never became ready or publicly accessible. Its bounded
[failure receipt](full-import-attempt-1.json) and
[source classification](invalid-pds-source-receipt.json) are retained. The source
files were reused on the same cloud volume and retired after full admission. The failed partial
index was retired, reclaiming 3,997,097,984 bytes measured by filesystem free
space; see [retirement](partial-index-retirement.json). The replacement image
passed all five cloud suites plus an import of the exact failing source actor.
See [replacement image](replacement-image.json),
[cloud pilot log](cloud-pds-fix-pilot.log), and
[source witness recovery](real-source-pds-recovery.json).

The full admitted index contains all 12,816,605 source actors and 598,327,062
lexical memberships. The snapshot has 9,929,022 missing PDS values and one
unscopable value; PDS coverage is a source limitation. See
[full admission](full-admission-and-replay-start.json). An initial
[footprint observation](footprint-observation.json) is about 4.6 GB versus the
native snapshot's 7.33 GB prefix artifact. This compares logical index bytes,
not allocated disk or RAM, and does not establish long-term merge size.

Live replay reached a retained tombstone whose source actor was recreated with
the same second-resolution timestamp. The fixed read-only tombstone selector
joins the current actor in the same SQL snapshot, suppressing only retained
notifications superseded by a nonempty actor at an equal/newer version within
the cycle cutoff. Raw tombstone cursors still advance, and skipped notifications
are counted. Backend equal-version and cursor conflicts remain strict. See
[source evidence](tombstone-conflict-preflight.json),
[indexed query preflight](tombstone-join-source-preflight.json), and
[independent review](retained-tombstone-review.md). The replacement was deployed and caught up before publication; no source records
were edited. The policy is `turso-current-state-cutoff-v1`. A later equal-version
restore after a deletion has already committed still fails closed with 409.
The 300-second overlap does not recover arbitrary backdated writes, and the two
source streams do not share one global transaction.

Full import temporarily used two performance CPUs and 4 GiB. The completed load
ran against the final two shared CPUs and 1 GiB from a separate cloud generator
(one shared CPU, 256 MiB, no volume or registered service, bounded lifetime).
[Load results](load-results.json) contain 4,749 normal-rate requests with zero
failures, including aliases, invalid inputs, exact/multiword/CJK queries, hard
PDS scope and preference. All latency figures below include client queue time.

| Phase | Requests | p99 |
| --- | ---: | ---: |
| Post-restart varied queries | 37 | 54.315 ms |
| Distinct derived handle prefixes | 512 | 390.279 ms |
| Warm queries | 1,200 | 12.936 ms |
| Five minutes during live replay | 3,000 | 13.004 ms |

The warm/live latency gate passed. Diverse first-touch p99 exceeded 100 ms and
remains a performance gap; restart measurements do not establish physically cold
disk. A separate 256-request, 32-client overload burst produced 147 expected
admission rejections, 109 accepted requests, no unexpected failures and successful
recovery. These rejections are excluded from normal-rate latency.

[Source progress](source-progress.json) verifies 293 committed actor mutations
and 1,075 replays across five fully contained cycles during the five-minute phase;
469 mutations were counted across eight fully contained cycles over the entire
load. There were zero errors since the telemetry baseline. These are conservative
logical record counts, not empty cursor commits or physical write amplification.

[Resource telemetry](resource-summary.json) covers the load window: peak server
RSS 695,300,096 bytes, no swap or OOM kills, 6,617 VM-wide major faults, and about
0.60% I/O pressure over the 866-second sample. Server reads/writes were roughly
784/51 MB. RSS includes mapped pages; VM-wide faults/pressure include replay and
telemetry, and the reported available memory includes reclaimable cache.

At load completion, 9,935,841 actors lacked PDS metadata and one had an unscopable
value, out of 12,836,956 current indexed actors. Filtering and preference work for
recorded canonical hosts; this does not establish recall for the roughly 77% with
unknown hosts. Long-term churn/merge behavior and native moderation refresh remain
cutover gates. See [qualification](qualification.md) for remaining work.

The [standard engine benchmark](engine-bench.json) also completed 44 queries with
three runs and stress disabled. From the operator connection, warm p50/p95 was
43/67 ms and varied-query p50/p95 was 44/70 ms. Its “cold” label means varied
requests, not a cold physical disk. The separate [ranking benchmark](ranking-bench.log)
passed structural and named-query parity checks, but broad top-10 overlap with
Bluesky averaged 47%, below the repository's 75% aspiration. Passing the named
checks does not establish equivalent overall relevance.

## Operational commands

Run from the repository root. The deployment controller rejects protected machine
IDs and requires the owned dev app's metadata. Its short-lived credential is
stored outside the repository; commands never print the value.

The initial controller credential has been revoked. Further control operations
require authenticated Fly CLI access to mint another short-lived app token;
protected inventory checks require account access. Do not reuse expired tokens.

```sh
python3 deploy/tantivy-dev/control.py status
python3 deploy/tantivy-dev/check-protected.py
```

After full import admission and fresh source readiness (at most 120 seconds lag),
`control.py publish` changes only this dev machine to the final serving shape
and registers its HTTP service. It does not change the production route.
Then `run-load.py start` launches one bounded cloud job; `run-load.py collect`
retrieves small result receipts from that same job. Never restart a load job
merely because observation timed out. Reconcile its actual machine state first.
After the final-size restart, run `stage-helpers.py` to install verified small
qualification/telemetry tools, and `measure.py start` before starting load.
After collecting load results, `measure.py collect` stops the identified sampler
and captures bounded resource summaries, including coverage of the load window.
The sampler runs only on the named dev machine and has a 20-minute maximum.
Load includes 512 distinct derived handle prefixes and a bounded 32-client burst;
expected admission rejections and recovery are separate from normal-rate latency.

Bootstrap/pilot failures stop without automatic retries. Only the admitted
published dev service uses three bounded failure restarts; ordinary boots reuse
the admitted index and durable replay cursor. See Fly's
[restart policy](https://fly.io/docs/machines/guides-examples/machine-restart-policy/).

Temporary downloaded source files are removed only after verified full import.
The admitted dev index and its replay state are intentionally retained for user
testing. [Final lifecycle](lifecycle-final.json) confirms the load helper is gone, source
payloads are retired, the sampler is stopped and the dev volume has
35,171,954,688 free bytes. The [controller credential](controller-cleanup.json)
was revoked and its private file deleted; runtime credentials are unchanged. Source version ambiguity, deletion retention, moderation
refresh integration and long-term merge behavior remain separate cutover gates;
no success here authorizes production routing changes.

See [topology.md](topology.md) for all machine roles and data/control paths,
[plan.md](plan.md) for qualification scope, [resources.json](../../../deploy/tantivy-dev/resources.json)
for owned dev resources, [build-receipt.json](../../../deploy/tantivy-dev/build-receipt.json)
for the pinned image, and [protected-latest.json](protected-latest.json) for the
latest production/rollback/ingester and route comparison.

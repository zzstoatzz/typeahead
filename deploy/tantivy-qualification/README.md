# Matched engine qualification

The paired runner performs bounded HTTP screening through explicit loopback
evaluators/tunnels. The private Linux image includes the native batch writer and
Tantivy, and its tiny offline pilot checks preparation, committed updates,
restart and replay under a two-CPU/1-GiB container limit. These synthetic local
receipts establish lifecycle behavior, not provider resources or engine speed.

Cloud orchestration uses a separate qualification app and write-ahead ownership
ledger. A private cleanup worker must attest that it is running before any engine
volume is allocated. Engine and generator guests start paused until their
provider receipts and private targets are configured. No production call, live
source credential or native corpus enters the image. Temporary platform
capabilities remain outside the repository and receipts.

The previous RAM pair colocated client/server, completed different numbers of
writes, and lost unattended cleanup supervision. Its 2 GiB arm still failed
mixed-load tails. The existing Tantivy run used a separate generator and passed
4,749 normal requests, but diverse-prefix p99 was 390 ms. Neither receipt supplies
a matched native/Tantivy speed ratio. This trial must use the same workloads.

## Resource and time bounds

Start with a cloud operational pilot before any full-corpus run. Each engine
uses two shared CPUs, 1 GiB, an independent volume and no production route.
A separate generator uses one shared CPU/256 MiB, no volume/public service.
Cloud copies stay cloud-local. The native control comes from immutable R2,
never a protected serving/rollback disk. Snapshot download, build and import are
preparation, measured separately; do not call a 32-second load a 32-second pilot.

| Stage | Initial bound |
| --- | --- |
| Tiny preparation and lifecycle pilot | 32–100 actors, at most 60 requests per arm, at most 90 seconds of measurement per arm; 5-minute preparation deadline |
| Full preparation after pilot passes | One snapshot per independent cloud volume; at most 60 minutes per arm, including the native arm’s independent raw-profile oracle; 20 GiB throughout, with at least 1 GiB free |
| Full measurement | One pass per engine, at most 15 minutes per arm, 10 requests/second, concurrency at most 4, response budget 512 KiB, request deadline 3 seconds |
| First touch | 512 frozen distinct prefix/query cases, once each, 10 requests/second; no search seeding on measured index beforehand |
| Warm | 1,200 frozen requests over 120 seconds at 10 requests/second |
| Mixed | 3,000 frozen requests over 300 seconds, same frozen update feed and schedule for both engines |
| Restart | One same-shape restart per arm with durable cursor/state checks, bounded separately at 120 seconds |

Bounds are proposals for the parent cloud controller, not authorization to exceed
its budget. No host-swap repeat by default. Diagnose failed gates first; repeat
only if measured shared-host scheduling or other ambiguity justifies the cost.
Use a cloud-side supervisor with an unconditional teardown trap and an independent
expiry/reaper; the old laptop supervisor is not a reliable cleanup mechanism.
Retain bounded JSON receipts; remove temporary machines, volumes, payloads and
controller capabilities after collection. Protect any intentionally retained dev
service with separate ownership identifiers.

Fly's API retains destroyed machine records and soft-deleted `pending_destroy`
volumes. Cleanup validates their recorded owner/name/time bounds and unattached
state, then records those terminal dispositions. Pending-destroy volumes are
[not billed](https://docs.fly.io/about/billing); physical purge is managed by Fly
and is not claimed by the cleanup receipt. A new run ignores only validated
terminal resources from its own archived runs, never unknown live resources.

## Full-corpus runner

`image/full_prepare.py` downloads the two pinned R2 objects directly into each
owned Fly volume, verifies hashes and admits native data or imports Tantivy.
`image/full_bootstrap.py` keeps a pristine baseline and runs the same nine-stage,
64-actor update feed on each working engine. The native arm also streams all raw
profiles through `experiments/tantivy-oracle` to derive 512 query expectations.
Neither search engine's responses determine those expectations. The source is a
frozen snapshot; this component cannot qualify live ingestion or policy freshness.

`image/full_generator.py` measures the two arms sequentially, starting each
engine only when it is ready to measure. It checks actual Machines/Volumes API
records against runtime identity before each phase. Every request contributes to
latency and failure counts, including incorrect native results. Tantivy must have
zero truth/transport failures, meet 50 ms p95 / 100 ms p99 in warm and mixed phases,
and match or beat native shared-feature p95 and p99 in every phase. Missing
resource evidence, OOM, swap, insufficient disk reserve or telemetry gaps fail the
gate. Raw request/resource details remain on the temporary generator; bounded
summary receipts are retained from logs before cleanup.

The controller's explicit `--mode full --seconds 5400` selects this path;
default mode remains the tiny pilot. The independent reaper starts before either
volume exists and expires the entire run after 90 minutes. Download capabilities
are staged platform secrets limited to GET of the two immutable R2 objects. The
image and receipts contain no source credentials. A passing result remains only
the performance and frozen-feed component of the broader cutover qualification.

## Inputs and API contract

Manifest schema 1 binds four files by exact SHA256: immutable corpus descriptor,
query list, frozen update feed, and independent raw-profile oracle. The corpus
file can be a small verified export descriptor including source snapshot, complete
actor count, actor-stream hash, normalization/scoring version and source watermark;
actual corpus bytes remain in cloud storage. The provisioning observer must verify
that descriptor against each installed engine. Query paths must be `/search`.

An example query file:

```json
[{"id":"exact","path":"/search?q=alice.test&limit=10"}]
```

Oracle values are ordered DID arrays and HTTP status for each engine. Different
expected results need an explicit `intentional_difference` explaining the native
recall defect or intended semantic change; absence of equality is never accepted
just because the new engine returned something plausible. Generate expectations
from raw profile predicates, not either engine's candidate lists or response
capture. Include normalization, exact priority, broad conjunction, scope,
preference, hidden/deleted, rename, rescore, PDS migration and tie cases. The old
synthetic NFKC oracle does not establish production normalization parity.

```json
{"exact":{"native":{"status":200,"ids":["did:fixture:a"]},"tantivy":{"status":200,"ids":["did:fixture:a"]}}}
```

Each loopback evaluator must expose GET `/__evaluation`, forwarding `/search`
to exactly its isolated engine. Identity must include matching `nonce`, `arm`
(`native` or `tantivy`), machine/volume IDs, `guest` exactly
`{"cpu_kind":"shared","cpus":2,"memory_mb":1024}`, `ready:true`,
`corpus_sha256` and `updates_sha256`. These are *observer attested* values; an HTTP
self-description alone does not prove actual provider configuration. The manifest
contains both arms, their loopback origins, a distinct `load_generator_id`,
`native_source:{immutable_r2:true,protected_disk_used:false}` and bounded phases.
Machine/volume identities must match observer receipts and be disjoint from all
protected resources. The built-in historical-ID denylist supplements the parent's
fresh provider inventory; it cannot replace that inventory.

Example manifest skeleton (fill actual paths/hashes/identities):

```json
{
  "schema":1,"mode":"pilot","nonce":"unique-run-id",
  "inputs":{
    "corpus":{"path":"corpus.json","sha256":"..."},
    "queries":{"path":"queries.json","sha256":"..."},
    "updates":{"path":"updates.json","sha256":"..."},
    "oracle":{"path":"oracle.json","sha256":"...","method":"independent-raw-profile"}
  },
  "native_source":{"immutable_r2":true,"protected_disk_used":false},
  "load_generator_id":"isolated-generator-id",
  "arms":{
    "native":{"url":"http://127.0.0.1:18080","machine_id":"native-id","volume_id":"native-volume","guest":{"cpu_kind":"shared","cpus":2,"memory_mb":1024}},
    "tantivy":{"url":"http://127.0.0.1:28080","machine_id":"tantivy-id","volume_id":"tantivy-volume","guest":{"cpu_kind":"shared","cpus":2,"memory_mb":1024}}
  },
  "phases":[{"name":"pilot","attempts":32,"rps":4,"concurrency":2,"deadline_s":20}]
}
```

```sh
python3 -m unittest discover -s deploy/tantivy-qualification -p 'test_*.py'
python3 deploy/tantivy-qualification/qualify.py /cloud/run/manifest.json --validate-only
python3 deploy/tantivy-qualification/qualify.py /cloud/run/manifest.json --output /cloud/run/screen.json
```

The runner verifies identities before any search and between phases, counts every
failure, includes scheduled client queue time, rejects an existing output, and
compares each arm with its declared independent expected results. It runs arms
sequentially to keep generator CPU from competing; feeds must be reset and replayed
relative to each arm's mixed-phase start. Different shared hosts remain a confound.
The speed screen requires Tantivy p95 and p99 no worse than native in **every**
phase, including first touch. Small pilot percentiles have no statistical power.
A failed correctness gate prevents a speed-screen pass. Physical-disk cold state
is not guaranteed by restart or untouched query strings.

## Required orchestration before full qualification

The current runner always reports `full_qualification:false`: it intentionally
cannot manufacture missing mutation, restart, lifecycle or resource evidence.
The following must be implemented/collected by cloud orchestration before claiming
cutover readiness:

1. Freeze the same initial actor state and complete update feed. Direct live Turso
   polling differs by timing and is unsuitable for paired attribution. Root/native
   adapter must ingest the same full records/tombstones through its normal writer
   transaction path; Tantivy uses its authenticated loopback `/admin/apply` batch
   commit. The isolated native adapter uses the normal projection/removal helpers
   in one transaction, with durable feed/cursor barriers; `/admin/refresh` is not
   an equivalent batch/tombstone replay interface. Do not bypass normal writers or
   silently compare a batch writer with per-row commits. Record both transaction
   grouping and committed row/membership counts; batch-size differences are an
   explicit engine integration choice to evaluate, not hide.
2. Drive the frozen batch schedule independently of query client workers. Preserve
   target batch IDs/timestamps, applied/replayed/stale/conflict outcomes, each
   acknowledged commit cursor, backlog age and final effective actor-state digest.
   Both arms must drain the same feed through the same checkpoint within the mixed
   window. If one lags, compare lag and tail failure rather than calling less
   completed work faster. Report actual changes separately from cursor-only commits.
3. During mutation, time only stable expected-result witnesses with the current
   runner. At each deterministic update checkpoint, pause the feed and check all
   affected actors against the raw-profile oracle. Add explicit stale/replay,
   equal-version conflict, hidden/deleted, late restore, unknown-term/actor/PDS,
   old-membership suppression and PDS move checks. Concurrent dynamic queries need
   a versioned snapshot oracle; do not use one static expected list across changes.
4. Restart each same-shape engine once. Verify durable actor state, version barriers,
   cursor and query responses, then replay the last acknowledged batch and verify
   idempotence. Record readiness downtime separately from ordinary latency.
5. Independent telemetry must span the entire load: RSS/mapped/heap distinction,
   available memory, OOM/swap, major faults, CPU steal/throttling, memory/I/O PSI,
   block bytes, engine logical/physical writes, segment/merge progress and disk use.
   Include generator queue/CPU. Compare matched intervals, not cumulative counters
   from different lifetimes. Zero failure and matched update completion are required;
   retain absolute latency gates alongside native-relative speed gates.
6. Collect provider identities and final resource inventory, verify protected slots
   and route unchanged, then destroy owned scratch. No production cutover follows
   automatically from passing this experiment.

This lane tested real local HTTP transport, hash tampering, ownership mismatch,
protected/colocated targets, wrong oracle results and intentional recall divergence.
It does not assert that the existing public dev service already meets the matched
native speed requirement.

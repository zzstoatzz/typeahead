# Private matched-engine qualification image

This lane prepares a local Linux image, never provisions or pushes it. One runtime
starts **either** the native evaluator plus its frozen batch writer **or** Tantivy.
Both use the same private Python HTTP hop to loopback port 18080. No production
server route changes, live Turso polling or corpus download occur in this build.

The base is exactly the dev image's pinned Python 3.12 slim digest. Native SQLite
is compiled into the binaries: ELF dynamic-table inspection and an offline
container loader check verify that no extra system SQLite package is required.
`loader-results.json` records the actual dependencies. The slim base lacks
`readelf`; Xcode `llvm-objdump -p` provides the equivalent ELF dynamic-table check.

## Local preparation

```sh
python3 deploy/tantivy-qualification/image/build.py --docker --tag typeahead-qualification:native-local
```

Every Zig invocation is bounded to 360 seconds; artifacts stay in ignored `.build`.
The default builds native tools and a native-only context. It never compiles Rust.
After the coordinator freezes a Linux Rust binary:

```sh
python3 deploy/tantivy-qualification/image/build.py --skip-native-build \
  --rust-binary /absolute/frozen/typeahead-tantivy --rust-sha256 EXACT_SHA256 \
  --docker --tag typeahead-qualification:paired-local
```

The copied binary must match the supplied hash before and after copying. The
context includes native `eval-server`, `qualification-native`, `typeahead-ingester`
and `admit-prefix`, optional frozen `typeahead-tantivy`, and bounded cloud-local
export/fixture helpers. No credential files, native corpus, indexes or actor
payloads enter the build context. Nothing is pushed. `image-build.json` declares
which engines are actually present; native-only does not qualify Tantivy.

The tiny pilot runs `pilot_bootstrap.py` on each isolated engine and `generator.py`
on a separate machine. Both prepare the same 32 profiles and one rename batch;
the generator checks the old/new handles, restarts the serving processes and
replays the committed batch. The private control listener accepts only these
fixed actions. Its `QUAL_CONTROL_SECRET` capability is separate from the public
run nonce, and is supplied through a temporary platform secret alongside the
loopback `ADMIN_SECRET`. Neither capability enters receipts or image layers.

`python3 deploy/tantivy-qualification/image/test_pilot.py` exercises those exact
guest programs offline under a two-CPU/1-GiB container limit. Provider identities
are explicitly synthetic there. Under local x86 emulation, `/proc/PID/exe` can
identify the emulator; the receipt reports that mismatch rather than claiming a
verified engine process. Fly requires the executable hash to match the frozen
engine binary. This pilot supplies lifecycle evidence, not full-corpus latency
or resource qualification.

```sh
python3 deploy/tantivy-qualification/image/test_guard.py
python3 deploy/tantivy-qualification/image/test_proxy.py
python3 deploy/tantivy-qualification/image/test_linux_loader.py
python3 deploy/tantivy-qualification/image/test_container_native.py
```

`container-native-results.json` additionally exercises the Linux image with an
actual 32-actor build, guarded native runtime, private proxy and a committed frozen
rename. It uses Docker `--network none`, an independent 150-second container
deadline and automatic container removal. It does not attest provider hardware.

The guard test requires a host-native evaluator in `.build/host/bin` and the prior
host fixture builder under `experiments/tantivy-qualification-native/.tools/bin`.
Build the evaluator with `zig build eval-server -Doptimize=ReleaseSafe --prefix ...`
under `services/`, with a 360-second subprocess deadline. It creates 32 synthetic
actors through the actual builder and checks real HTTP search plus admission
failures. Scratch is removed and children are stopped; receipts are synthetic
local functional evidence, never provider or performance attestation.

## Private runtime manifest

Stage a root-owned mode-0700 directory, normally `/data/qualification/run`, with
an `owner` file containing exact bytes `native-qualification-v1`. All run/config,
snapshot/prefix, overlay, scratch, inbox and response paths must be canonical
children, with no symlink escapes. A new `typeahead-qual-` Fly app is required;
three protected machine IDs and `/data/refresh` paths are rejected before any
native overlay open. Existing legacy evaluator behavior remains separate. Old
looping background-writer feeds are forbidden in qualification mode.

The controller writes a normalized observer receipt from its actual Machines API
read. The image verifies its bindings; the controller must independently retain
and compare the actual provider response. Self-reported runtime fields alone
cannot establish ownership or guest shape.

```json
{
  "app":"typeahead-qual-example",
  "machine_id":"OWNED_MACHINE_ID",
  "guest":{"cpu_kind":"shared","cpus":2,"memory_mb":1024},
  "volume_ids":["OWNED_FINAL_VOLUME_ID"],
  "final_volume_gib":20,
  "public_services":[],
  "machines_api_response_sha256":"ACTUAL_PROVIDER_RESPONSE_SHA256"
}
```

`run.json` selects exactly one engine and binds its frozen input identities:

```json
{
  "schema":1,"arm":"native","root":"/data/qualification/run",
  "app":"typeahead-qual-example","machine_id":"OWNED_MACHINE_ID",
  "volume_id":"OWNED_FINAL_VOLUME_ID","final_volume_gib":20,
  "guest":{"cpu_kind":"shared","cpus":2,"memory_mb":1024},
  "nonce":"UNIQUE_16_TO_128_BYTE_RUN_NONCE",
  "corpus_sha256":"64_lowercase_hex_characters",
  "updates_sha256":"64_lowercase_hex_characters",
  "observer_receipt":"/data/qualification/run/observer.json",
  "private_listen":"fdaa:PRIVATE_MACHINE_IPV6",
  "measurement_deadline_s":900,
  "paths":{
    "snapshot":"/data/qualification/run/index.db",
    "overlay":"/data/qualification/run/overlay.db",
    "tmp":"/data/qualification/run/tmp",
    "request":"/data/qualification/run/request.json",
    "response":"/data/qualification/run/response.json",
    "tantivy_data":"/data/qualification/run/tantivy"
  }
}
```

Bind only a `fdaa:` IPv6 address, or IPv6 unspecified `::` in the explicitly
private Fly app with **no public services**. The runtime checks actual Fly app and
machine environment without deleting or disguising it. It rejects inherited live
Turso environment. For Tantivy, the private loopback admin capability arrives at
runtime via `ADMIN_SECRET`, never in a build layer or receipt. Admin is not
forwarded. Preparation uses explicit tool invocations from the controller; the
entrypoint never downloads or imports source automatically.

The entrypoint starts the chosen engine, waits up to 60 seconds for its health
endpoint, then starts the native writer if selected. This avoids two independent
SQLite initializers racing on an empty overlay. The shared proxy forwards only
search/health/readiness/status/debug and the two XRPC aliases. HTTP headers are
bounded to 4 KiB and GET responses to 512 KiB; POST and admin paths are rejected.
Response bytes and PDS acknowledgment are preserved, without caching. Port 8080
is private. Backend and authenticated admin bind loopback ports 18080/18081.

`/__evaluation` includes nonce/arm, control machine/volume namespaces, input and
binary hashes, and the observer-receipt hash. Its readiness reflects the actual
engine health. It explicitly declares `source_kind:frozen-synthetic-controller`
and `source_ready_attestation:false`. The frozen controller must commit identical
ordered feed steps to both arms. Any source-owned heartbeat may advance only
after those steps commit, using a complete frozen policy refreshed at the
synthetic run clock; this is never evidence of real Turso freshness. Tantivy runtime settings are explicit in `run.engine_settings` and evaluation
identity: defaults match its CLI (`writer_memory_mb:128`, `concurrency:4`,
`max_candidates:10000`). Record any deliberate tuning in the paired manifest. Strict
freshness is the default. Any `allow_stale`/`allow_partial` run setting is explicit
and retained in the run manifest; it cannot qualify actual source readiness.

## Cloud resource and lifecycle budget

The coordinator owns provider mutation and cleanup. First run a 32–100 actor pilot
on two separate two-shared-CPU/1 GiB/20 GiB engine machines, with a separate
one-shared-CPU/256 MiB generator. Verify isolation, ordered batch commits, oracle,
restart, telemetry and teardown before copying the full immutable R2 release.
No protected disks are cloned or used as scratch.

For full preparation, the known immutable prefix artifact is 7,331,160,064 bytes
plus an 81,920-byte metadata SQLite file. Export JSONL directly into Tantivy;
do not retain an unnecessary whole-corpus JSONL copy. Temporary preparation may
use 40 GiB per arm, explicitly accounted separately from serving. Reserve at least
8 GiB free before merge and stop rather than exceeding the 40 GiB budget. Preparation may temporarily use two performance CPUs and 4 GiB for import; record
that cost/time separately and restart on the final shape before measurement. Native
preparation has an initial 10-minute ceiling; Tantivy fresh 13-million-actor
import/merge has a 60-minute ceiling, reflecting the observed roughly 40-minute
preparation rather than the earlier unrealistic 20-minute estimate.

Final serving disks must each be **20 GiB**. Fly volumes cannot shrink: either
use independent temporary scratch with a final 20 GiB volume, or copy the admitted
index/cursor to a fresh final volume and verify them before retirement. A 40 GiB
preparation volume left serving is not a matched-disk result. Remove source and
merge scratch only after successful admission and retain small hashes/receipts.
Restart both final-size engines before measurement. Measurement is one bounded
pass per arm, at most 15 minutes, with separate generator and matched query/update
hashes. No host-swap repeat by default. These isolated workload results do not
automatically authorize production cutover or routing changes.

The runtime stops all child process groups on exit and after the measurement
bound. Infrastructure retirement remains the controller's responsibility, with
an independent expiry/reaper and a final provider inventory. The old laptop-only
supervisor did not provide reliable cleanup.

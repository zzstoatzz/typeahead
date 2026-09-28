# Bounded-lookup shadow canary

## Retired September 28, 2026

Deleted stopped machine `7841145a543e78` and its temporary 60 GB volume
`vol_vxmxpk7qgy070gn4` from `typeahead-canary`, removing approximately $9/month
in provisioned-volume cost. Fly confirmed both deletions. The machine had been
stopped since September 6; no public requests depended on it.

The volume held a restored, frozen evaluation fixture and disposable write-test
copies, not primary production data. Evaluation results, artifact hashes,
comparator source and pilot reports remain committed in `docs/evals/` and
`ops/canary/`. The exact on-volume fixture and runtime state were not archived.

The bounded lookup shipped September 9. A September 28 read-only `bs` probe
confirmed `overlay_bounded: true`, 59 returned `d:bs` overlay rows, and a
follow-up HTTP 200 response in approximately 95 ms including network time.
This is a spot check, not a latency benchmark. The separate ranking-index
experiment remains deferred: sustained sync/write cost and memory headroom
still need validation before promotion.

The instructions and resource IDs below are historical. Resuming the experiment
requires provisioning a new isolated fixture and machine with new expiry and
admission limits; the old start/proxy instructions are not directly runnable.
The empty Fly app was retained.

## Historical pilot

September 6, 2026 UTC: the deployed shadow pilot completed successfully.
Admission is paused and its machine is stopped. Public search and ingestion
deployments are unchanged. No candidate answers have been served to users.

## What receives traffic

A time-limited `wrangler tail` session samples 10% of successful GET invocations.
`sample_live.py` accepts only the two public search endpoint paths, sanitizes the
query, and sends at most one query every 16 seconds through a private Fly proxy.
It includes edge-cache hits; it is not a representative sample of origin-only
traffic. The native evaluator compares the entire ranked candidate list before
HTTP `limit` truncation, rather than just the caller’s requested first few rows. Tail delivery itself is best effort. No Worker code/config changes,
public canary endpoint, extra Turso reader, or recurring monitor are required.

The isolated executable runs the old and bounded implementations in one read
transaction against the same frozen database and snapshot. It compares ordered
actor JSON and scores, returning only parity, duration and row-count metrics.
Live queries are fresh; the fixture is frozen. This tests lookup equivalence,
not live freshness, production end-to-end latency, or ongoing sync capacity.

The pilot stops after 20 admissions or ten minutes. Normal exit, interruption,
or a detected comparison failure pauses admission and waits for the in-flight
comparison. Killing the sampler stops query delivery; the server independently
limits work and expires even if the laptop disappears. Raw tail events, query
text, caller identifiers and actor records are not written to reports. Wrangler's
local debug log is directed to a temporary symlink to `/dev/null`.

## Limits and controls

Server limits: one comparison in flight, at least 15 seconds between admissions,
120 admissions total across restarts. Failure or mismatch auto-pauses admission;
restarts always start paused. Each child has a 15-second wall deadline, 10 CPU
seconds, 1.5 GiB address-space limit, no core dumps and no provider credentials.
SQLite reads have the production one-second busy timeout. The server exits at
**2026-09-06 07:43:27 UTC** and the machine's restart policy is `no`.

The machine is stopped after the pilot. A new pilot requires an explicit
canary-only machine start (and a new expiry if the old one has passed); the
server starts paused and retains its total admission count. Do not enable
autostart or an automatic retry loop.

When the canary is running, start the private proxy from the repository root:

```sh
fly proxy 8898:8080 -a typeahead-canary
```

Then use the dedicated credential loaded in memory from the encrypted store:

```sh
python3 ops/canary/sample_live.py status
python3 ops/canary/sample_live.py sample --seconds 600 --count 20
python3 ops/canary/sample_live.py pause
```

`pause` rejects new comparisons; an already running child can finish within its
15-second bound. This rollback is independent of SQLite DDL. Stop the sampler
before manually pausing it. For immediate compute shutdown:

```sh
fly machine stop 7841145a543e78 -a typeahead-canary
```

No user requests depend on this machine. Do not run DROP INDEX or any canary
DDL on the production search machine. Keep the canceled health monitor paused;
use one-off production availability, latency, freshness, queue and throttle
checks while actively operating the pilot.

## Deployment and fixture

| Resource | Isolated value |
| --- | --- |
| Fly app / organization | `typeahead-canary` / `personal` |
| Machine | `7841145a543e78` (`ranking-eval`) |
| Allocation | EWR, 4 shared CPUs, 2048 MiB |
| Volume | `vol_vxmxpk7qgy070gn4`, 60 GB, backups disabled |
| Restore source | existing snapshot `vs_Lb7XmALlZ8gyUgxo4DaX6KyY` |
| Entrypoint | `python3 /data/canary/server-v2.py` |
| Runtime image | Python 3.11 slim Bookworm; Fly configuration pins its digest |
| Listener | private IPv6 port 8080; no public service or IP |
| Overlay | `/data/local.db`, watermark `1788585049` |
| Snapshot | `/data/index/index.B.db`, build `b1788512492-0ed0` |
| Comparator | `/data/canary/eval-lookup` |
| State | `/data/canary/state.json` |

The restored overlay passed SQLite quick_check: 11,158,428 prefix rows and
251,841 actor rows. The snapshot has 11,631,204 actors. `canary_rank` is installed
on the restored source; actor/prefix contents are unchanged. `/data/eval/plain.db`
and `ranked.db` were deliberately mutated by write tests and are not pilot inputs.
The original source was restored from an existing volume snapshot, avoiding a
live production export or additional Turso sync.

The native executable is a separate optional build target:

```sh
cd services
zig build eval-lookup -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
```

`compare.py` and `limit_exec.py` supervise it. `server.py` is the canonical
HTTP/control implementation; `server-v2.py` is its deployed copy on the volume.
The normal service build does not install the standalone evaluator. Deployed
file hashes were checked against the [tested artifacts](../../docs/evals/canary-artifacts.json).

## Evidence and remaining promotion gates

[Full evaluation](../../docs/evals/bounded-overlay.md) and
[measurements](../../docs/evals/full-overlay-summary.json):

- All 36 full-data query cases match, on both plain and indexed copies.
- The fixed comparator passes 108 comparisons concurrent with actor writes.
- Index size: 569,638,912 bytes (543.25 MiB); build: 97.663 seconds. Existing
  free pages absorbed it, so the database file did not grow in this fixture.
- Five real committed 500-actor projection batches took 18.45 seconds without
  the index versus 31.38 with it, including test setup. Observed peak WAL was
  roughly 42 versus 117 MiB. These single batches are not a steady-state sync
  benchmark. The index has a real write cost.
- Unit/integration checks cover authentication, bad input, rate/concurrency
  limits, auto-pause, restart, expiry, malformed output and child timeouts.
  Real lock tests cover brief-lock success and bounded persistent-lock failure.
- Private deployed checks verified unauthenticated rejection, admission,
  rate limiting, real parity, pause and reactivation. A sampler interruption
  stopped delivery and left admission paused with no child running.

Per-process measurements run baseline first, warming caches for the candidate;
do not publish their ratio as a production speedup or call them production P95.
Before serving candidate answers, test sustained overlay-write catch-up and
memory headroom with the index, prepare an offline artifact/promotion path, and
use a small reversible serving rollout. Passing this shadow pilot alone does
not establish those gates.

## Initial live result

The [ten-minute pilot](../../docs/evals/live-shadow-pilot.json) observed 42
eligible sampled searches, admitted 18 under its pacing limit, and passed all
18. An [earlier interrupted pilot](../../docs/evals/live-shadow-interrupted.json)
passed two live samples and verified interrupt cleanup. Including the private
`bs` smoke check, the server ended with 21 accepted / 21 passed / zero failed,
admission paused and no child running. The isolated machine was then stopped.
This is an initial safety/parity result, not statistical proof of all queries.

During the pilot, production checks showed P95 around 23–24 ms, only HTTP 200
responses in the measured windows, no throttling or ingest backpressure, and
passing freshness. The [canary resource check](../../docs/evals/canary-resources.json)
showed no throttling, about 1.7 GiB memory available, and 14.3 GiB disk available.
Artifact hashes matched the deployed files. The local tail and private proxy
were shut down after the run; no recurring job was installed.

## Credential and lifecycle

Only a dedicated `CANARY_TOKEN` is on this app. Its source is the encrypted
`prod.yaml` key `local.typeahead_canary_token`; the CLI loads that same key. No
Turso, R2, Logfire or production search credentials were copied to the machine.
Rotation means replacing that store key and importing the derived Fly secret
via stdin, followed by applying the staged secret to this machine and verifying
an authenticated status call. No Worker secret is involved. Never print values.

The server's expiry stops compute, not volume charges. This temporary 60 GB
volume must be explicitly deleted when the pilot fixture is no longer needed;
stopping a machine does not delete its volume. Preserve metrics first. Do not
retain a second full fixture indefinitely as a monitoring solution.

# Storage follow-up — October 27, 20:25–20:35 UTC

The goal remains incomplete. The routing proof trial was restored to retained
40a014e4 at 20:37 UTC. It did not establish the public target. The fixed window
recorded 151 misses, 48,087 ms (318.46 ms mean), 18 hits and 171 HTTP-200 answers:
two answers do not reconcile with latency counters and cannot be treated as
successful zero-time requests. Existing sampling cannot attribute that gap to
specific bodies. The failed and corrected queries both remain in route-proof/.

The query helper initially put `limit` at the top level, where it did not expand
calculation groups. The corrected documented `parameters.limit=100` returns
27 groups for the identical original window. There are 57 matched-minimum-count
observations across ten locations: weighted candidate mean 318.70 ms, retained
360.61 ms. These small natural-traffic cells still have query-mix differences
and large outliers. This does not qualify a <=169 ms public mean, and no full-
traffic promotion followed. Both backends and the routing object are unchanged.

Read-only Fly ranges for 20:20–20:40 UTC show mean CPU work about 0.040 cores,
zero CPU throttle, and CPU burst balance near its maximum. Volume busy time
averaged 27.8%, peaked at 90.7%; mean I/O-wait was 0.380 cores. Read service time
peaked at a one-minute mean of 12.26 ms and write service time at 115.26 ms.
These are device-counter-derived means, not request latency percentiles or proof
of a device quota. The ranges are retained in ../route-proof/canary-retry/fly-pressure.json.

The same-window sync spans separate the earlier ambiguous apply cost: 28 batches
averaged 1,058.61 ms projection and 8,454.04 ms commit. Old-row lookup averaged
347.71 ms, scope checks 20.54 ms and overlay work 688.39 ms (nested inside
projection). Slow-search logs include 2,213 ms with 1,947 ms in immutable-base
hydration, and 2,153 ms with 1,527 ms in that phase. The 1-in-10 sampled search
spans include a 3.16-second maximum. These samples do not establish a population
percentile. They show that the earlier frozen test does not cover all current
storage interference. The current overlay has 53,610 actors versus 18,200 in the
qualified frozen capture. Commit may include automatic checkpoint work; its
phase duration alone cannot distinguish checkpoint writes from fsync or host I/O.

The next local candidate increases the existing immutable-page cache from
128 to 512 pages per reader (1 to 4 MiB). The default four-reader pool therefore
budgets 16 MiB of page payload instead of 4 MiB, with unchanged generation reset,
block validation, ranking, query work, concurrency and durability. This is a
bounded attempt to avoid rereading recent pages while writes contend for storage,
not a claim to eliminate commit cost or cache the corpus. Native tests pass
154/160 with six platform skips; it is not deployed or performance-qualified.

Use an independent inactive-storage fork for subsequent qualification, retaining
both managed backends. The earlier frozen-fly/run.py source points at slot A,
which is now SERVING: do not reuse that old source-volume setting. A new trial
must guard current route/instances, select the inactive B source, use its own
volume/Machine and retain the input hashes and measured overlay size. Budget
20 GiB, bounded process supervisor and retirement on every exit. No corpus is
to be downloaded to the laptop. No new trial resource has been created yet.

API references: https://fly.io/docs/monitoring/metrics/ and
https://developers.cloudflare.com/api/resources/workers/subresources/observability/subresources/telemetry/methods/query/

The two-counter gap persisted on a later same-window confirmation at about
20:56 UTC; the original and confirmation snapshots are both retained. Linux
ReleaseSafe baseline/candidate binaries have now built successfully, identified
in build-pair.json. Their scratch root is /tmp/typeahead-cache512-pair-20261027
(owner: this goal, binary budget 64 MiB); retire it after the private image is
built or if this candidate is abandoned. No corpus payload is present locally.
The 20:55 public probe was healthy with 131-second lag, and the transport API
confirmed retained 40a014e4 alone.

## Cache qualification launch

Owner: this performance goal. The pending private run uses an independent fork
of inactive B volume vol_40o3xy5812d6kkk4, build b1793081565-0228-px05. At 20:55
B had 78,861 overlay actors; capture determines the exact frozen count and input
hashes. Baseline is native 62266a3, candidate is fb8907d (only the immutable-page
cache capacity differs). Both use identical evaluator and frozen workload.

The supervisor guards the production routing object and both original Machine
identities, creates no services, fixes the private guest to two shared CPUs /
1 GiB without swap, budgets a 20 GiB volume / 16 GiB payload / 16 MiB evidence,
and retires its Machine, volume and short-lived scoped token on every exit.
Scratch is the owned private volume's /data/refresh; evidence is /data/evidence
and this directory's run-ID subdirectory. The immutable snapshot stays in Fly.
Local image context /tmp/typeahead-cache512-image-20261027 contains only the
25 MiB ELF pair and small scripts; remove it and the binary pair after the image
is pushed. The image is an offline evaluator and is ineligible for publication.
The earlier source settings that referred to now-serving A are not used.

The first Docker push failed before publication because its stored registry
credential could not resolve this app repository. Normal Fly Docker
authentication was refreshed before retrying the identical tag; no runtime
deployment or production credential was changed.

### First run stopped by its guard

Run 20261027-26af11250a09 stopped during reference capture because its serving-
Machine read received HTTP 403. It produced no qualification result. The
Machine and independent volume were retired and its app token revoked. Fly's
`auth token --help` explicitly warns that the current CLI token may expire
quickly. The retry uses a named read-only personal-organization token with the
same 90-minute lifetime solely for fixed serving-Machine identity reads; the
app-scoped token still owns trial mutations. Both named credentials are revoked
on exit. This replaces the expiring CLI-token capture, not any data/resource
guard or acceptance criterion.

### Complete uncached-attempt accounting — local, not deployed

The public handler now schedules the latency counter once in a finally block
around its entire cache-miss path, including backend failure, scope-acknowledgment
failure and SQL fallback failure. A request that sanitizes to empty is also
counted as an uncached answer. Cache hits retain their separate counter; request
validation and emergency proxy behavior are unchanged. The legacy HTTP-200 empty
error response is preserved and still explicitly logged as backend_error, so
HTTP success alone cannot qualify it. Successful actor/ranking/cache behavior
is unchanged. Future public mean gates must include these failed attempts;
older counters omit them and remain labeled with that limitation.

`node scripts/snapshot_refresh/test_search_accounting.mjs` passes with real
workerd, Cache API, SQLite/D1 and a loopback HTTP origin. It checks the normalized
empty answer, real HTTP-500 origin wait retained in total_ms, uncached error
recovery, cache-hit separation, fail-closed PDS acknowledgment and an actual
SQLite fallback error counted once. Fixture subprocesses, server and temporary
files are retired in finally. The existing 104 Worker tests, typecheck, lint
and upload dry run also pass. This is not yet a production measurement.

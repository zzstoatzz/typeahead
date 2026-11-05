# Lifecycle qualification

Status: runtime fixtures passed at 05:41 UTC and controller fixtures at 06:08 UTC,
November 5, 2026. No runtime from
this record is deployed. The [cleanup plan](../../plans/lifecycle-cleanup.md)
defines completion.

## Verified so far

The [cloud result](cloud-result.json) records 36 passing tests, without skips,
in 40.8 seconds on the [observed guest](cloud-machine.json). The
[log](cloud-fixtures.log) covers real checkpoint/WAL export, import and source
handoff, immutable publication to a local fixture store, corruption rejection,
continuous polling, cached restart and stale-coverage serving with admission
rejection. All source HTTP requests in this run target synthetic loopback
fixtures; the guest has no production credentials or public services.

The exact [source package](package.json) contains 512,596 bytes. The
[preparation script](prepare-image.py) extracts committed source and hashes each
file; the [runner](run-fixtures.py) bounds the child process groups. Build with
`docker build --provenance=false --platform linux/amd64 --network none`, choosing
`--target runtime` or `--target fixtures`. The runtime layers only the slot script
and installer over the qualified continuous-poller image. Its unchanged reader
is `ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d`.
The inherited image revision label describes the base; use the package's file
hashes and source revisions for the overlaid scripts.

The test Machine `85e356c4360178` removed itself after exit 0. Independent lists
confirmed no Machine or volume remained, then the disposable app was destroyed
and confirmed absent. The same packaged tests also passed locally with network
disabled. Local scratch totaled 1.1 MiB; neither the laptop nor HeavyPad fetched
corpus data for this work.

These are fixture results. Full-data matched performance, managed admission,
public cutover and a completed production refresh remain unverified for the new
runtime. No release proof is authorized by this record.

The [source verification](source-verification.json) records commit-by-commit
checks for both repositories, plus 93 actual-reader publisher/controller checks
without skips. All three Typeahead CI workflows passed for `6ab4a59`. Publisher
cleanup is published on both mains at `a4ee1ef`; the registered deployment still
uses `52fe3e7` and the old compatible installer. Its CI is a separate gate before
any release pin changes. The [installed image hashes](image-files.json) identify
the exact candidate payload; the [bisect log](typeahead-bisect.log) records all
12 slot tests passing at each Typeahead change.

## Resource boundaries

Owner: this Typeahead lifecycle cleanup. Local scratch:
`/tmp/typeahead-lifecycle-20261105`, at most 5 MiB of source and metadata. Docker
uses the already cached production base; only changed scripts and synthetic test
helpers are uploaded. No corpus or hosting mapping is downloaded locally.

Cloud fixture qualification uses a disposable Fly app
`typeahead-lifecycle-20261105`, two shared CPUs and 1 GiB RAM, with no public
services, production credentials or persistent volumes. `/tmp` holds synthetic
fixtures only; budget 512 MiB. The test supervisor bounds execution to five
minutes and kills its child process group on completion or failure. The Machine
must use restart policy `no` and automatic removal. Fetch its bounded logs and
verify Machine removal, then destroy the temporary app. No recurring observer or
home workload is introduced.

This fixture run does not reserve either managed slot or authorize promotion.
Full-data candidate qualification follows the [preflight](full-data-preflight.md)
after the scheduled refresh, with independent storage and the existing release gates. Production remains
on the qualified continuous-poller image until then.

The root README was reviewed: these qualification steps change neither public
usage nor deployed architecture, so it needs no additional detail.

### Controller recovery fixtures

The [controller result](controller-result.json) records 82 passing tests without
skips, using the actual reader and installer on the observed
[cloud guest](controller-machine.json). Tests ran in 78 seconds; bootstrap plus
execution took 97.7 seconds. The [log](controller-fixtures.log) covers dead-slot
recovery, interrupted provider startup, durable resume, changed ownership and
routing, coverage expiry and promotion verification. These provider failures are
HTTP/filesystem fixtures; no real production provider mutation was attempted.

The separate disposable app, `typeahead-controller-20261105`, ran the committed
controller's tests while the real refresh built. It
has two shared CPUs, 1 GiB RAM, no public service, persistent volume or production
credentials. Its scratch is `/tmp` on the ephemeral root disk: allow 1 GiB for
public Python dependencies and small synthetic fixtures. No corpus is fetched.
The checksum-pinned uv bootstrap and dependencies download directly into that
cloud guest; the laptop uploads only the small test source layer.

The [runner](run-controller-fixtures.py) limits bootstrap and execution to ten
minutes, disables Prefect's ambient API/ephemeral server, and terminates its child
process group. Use restart policy `no` and automatic Machine removal. Preserve
bounded logs and source hashes, verify Machine/volume removal, then destroy the
temporary app. Provider failures use real HTTP fixture servers and owned local
objects; they do not exercise real provider mutations or authorize release.

The [source package](controller-package.json) was 730,164 bytes. During execution,
the cloud uv cache held 471,194,715 logical bytes and the ephemeral root retained
7,513,108,480 free bytes. Dependencies downloaded in the cloud, not through the
laptop or HeavyPad. The Machine exited 0 and removed itself; Machine and volume
lists were empty before app deletion, then the app was confirmed absent. Local
scratch, including both fixture packages and metadata, remained about 2 MiB.

## Matched comparison

The [comparison tool](compare.py) implements the [preflight](full-data-preflight.md)
cohort and bounds. Supply the actual machines and their shared build after managed
qualification, and use a new output path for each attempt:

```sh
python3 docs/ops/lifecycle-cleanup-20261105/compare.py \
  --baseline https://typeahead-tantivy-a.fly.dev \
  --candidate https://typeahead-tantivy-b.fly.dev \
  --baseline-machine BASELINE_ID --candidate-machine CANDIDATE_ID \
  --build-id SHARED_BUILD --output NEW_RESULT.json
```

It requires explicit managed origins, permits no redirects, and issues at most
39 requests per target at one request per second per target, with a five-minute
execution bound. Every response must acknowledge the expected engine, format,
binary, build, retrieval, machine and scope. Global requests cannot acknowledge
a PDS scope. Changed instances, errors, mismatched bodies and incomplete cohorts
fail qualification. Results contain hashes and timings, not full profile bodies;
existing output files cannot be overwritten. Sampled p95 uses nearest rank.

The [real HTTP tests](test_compare.py) passed all four cases in 81.6 seconds;
see [the test log](comparison-tests.log). They exercise the full paced cohort,
reject a fast network response that reports a slow engine, reject unexpected
scope and identity, preserve failed output, and stop redirects after one request.
Loopback execution requires `--test-loopback` and always records `test_only: true`;
it cannot substitute for the forthcoming real-backend comparison.

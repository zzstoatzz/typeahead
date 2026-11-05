# Lifecycle qualification

Status: cloud fixtures passed at 05:41 UTC, November 5, 2026. No runtime from
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
Full-data candidate qualification needs a separate preflight after the scheduled
refresh, independent storage and the existing release gates. Production remains
on the qualified continuous-poller image until then.

The root README was reviewed: these qualification steps change neither public
usage nor deployed architecture, so it needs no additional detail.

# Worker placement comparison

Owner is the Typeahead serving-path investigation. The scratch directory is
`.eval/placement/`, with a 10 MiB evidence budget. Temporary Workers are
`typeahead-placement-probe`, `typeahead-probe-regional` and
`typeahead-probe-default`. Delete the caller first, then both targets through
the Cloudflare API after retaining results. No cron is configured.

The two targets share one wrapper and import the production transport handler.
They use the existing R2 route and RouteAuthority. One has the production
`aws:us-east-1` hint, the other has default placement. Compatibility dates match
production to isolate the placement variable. Only the caller has an HTTP URL.

Phase one alternates eight echo pairs, reversing order on successive pairs.
Both targets return the same 3 KiB body immediately, without route or Fly calls.
Phase two alternates six search triples across regional, default and the
production transport, using three fixed queries with limit seven. Requests
are spaced at least five seconds after each response. Full response hashes,
build IDs and timing headers are retained. This is a finite diagnostic with at
most 18 backend searches, rather than a throughput benchmark.

The current serving slot and standby admission were read before the experiment.
The serving slot was healthy and the standby fit. The route is unchanged.

Johanna's GitHub default branch was checked again at
`f179358bef6118de9d87e1dffaf4b6835a270044`. The bounded-intersection change
`9d25850` uses a limit-scaled scan budget, with a regression test demonstrating
that the budget can omit a match. Her refresh sources fetch Slingshot identity
and profile data alongside AppView facts through a durable queue. These engine
and ingestion changes are independent of this forwarding experiment.

## Results

The caller entered Cloudflare at ATL. Eight alternating echo pairs returned
identical 3 KiB bodies. Regional placement had a 47.5 ms median call duration
(29–65 ms). Default placement had a 3 ms median, with individual durations of
20, 2, 198, 3, 2, 2, 3 and 4 ms. All echo handler timers read zero at Workers
clock resolution. The 198 ms default outlier is retained.

Six search triples returned identical response hashes and build IDs for each
query. Median caller waits were 108.5 ms for the fresh regional clone, 38.5 ms
for the default clone and 66 ms for the existing regional transport. The first
default search took 231 ms, including 91 ms route validation and 133 ms Fly
headers in serial mode. Later default searches took 36–40 ms. This experiment
establishes a repeated placement boundary cost, while also showing startup
and dependency tails that the median cannot describe.

The immediate-response A/B reproduces the former roughly 50 ms residual
without any backend or route work. Placement introduces forwarding overhead
before and after handler execution. These records do not separate network
travel from internal dispatch. The measured median difference was 44.5 ms for
echoes and 27.5 ms for default searches versus the production transport.
Request placement headers were absent, so the experiment does not assign a
physical execution datacenter from the inherited `request.cf.colo` value.

[Raw results](2026-11-16-placement-results.json) and
[pre-experiment admission](2026-11-16-placement-admission.json) retain evidence.
The production transport configuration now uses default placement. Its route
validation, speculative fetch, identity checking and fallback paths are unchanged.

The production transport was deployed as `0d1a8dfa-d0a9-460c-bfa2-23830645da8e`.
Settings readback reported an empty placement configuration and smoke passed.
Six public requests before and after, each a cache miss, retained identical
DID lists and build IDs. Their median caller/transport residual fell from
25 ms to 5 ms, Worker time from 60 ms to 45.5 ms, and client elapsed time
from 161 ms to 103 ms. The client comparison is sequential and includes
connection effects. The paired residual is the narrower verification.
[Before](2026-11-16-placement-public-before.json) and
[after](2026-11-16-placement-public-after.json) preserve all observations.

All three temporary Workers were deleted after capture. No trial process
remains. README was reviewed and its architecture overview remains accurate.

# Actor-search evaluation

`/eval` compares typeahead with Bluesky. The first view is a weighted benchmark
score, with dimensions, raw metrics and the test set available as drill-downs.
The website reads saved snapshots; it never starts collection.

Initially deployed unlisted on 2026-11-16 at [typeahead.waow.tech/eval](https://typeahead.waow.tech/eval)
from commit `2e41970`, Worker version `aaf2ce60-05f9-4e56-97b6-9397d0f6d89c`.
The initial run is imported. The live [run download](https://typeahead.waow.tech/eval/data.json?run=2026-11-16T07%3A25%3A35.153Z)
was verified equal to the retained seed, with all 80 observations. API and image
smoke checks passed; optional admin CRUD checks were not run without an admin
secret. Navigation is unchanged and the page has `noindex`. No slot, transport,
route or collection schedule was changed.

The timing disclosure and header preservation were deployed on 2026-11-16
from `7ae5d20`: public Worker `87c195f6-5664-4052-9952-f618135ee333`,
transport `53744bc8-4569-4f7b-9f32-c59e272e40ab`.
The [rollout evidence](investigations/2026-11-16-seek/timing-rollout.json)
records the live headers, unchanged 80-observation archive and verification.
Public API and image smoke checks passed; optional admin checks remained
unavailable without a secret. Phone and desktop views were checked in both
themes, and live cache hits were verified to omit historical engine timers.
No slot, route, placement or collection schedule changed.

## Retained history

The initial 2026-11-16 run is in
[`data/eval/runs/2026-11-16T07-25-35.153Z.json`](../data/eval/runs/2026-11-16T07-25-35.153Z.json):
80 measured responses, 40 per provider. Its observations are unchanged from the
original local report. The location label is generalized to `local laptop`, and
the query manifest is included. It is measured data, not a generated fixture.
The seed is deliberately outside the ignored `.eval/` directory so rollout can
import it into an empty store.

The portable runner retains new runs as immutable JSON files under `.eval/runs/`. Set
`EVAL_DATA_DIR` to a persistent volume on another host. `EVAL_LOCATION` labels
that runner's network location; use a distinct label when moving it. The preview
combines the seed and retained runs. The selector keeps locations and runs
separate; it does not blend laptop and cloud latency.

```sh
just eval-serve                      # http://127.0.0.1:18790/eval
just eval-import /path/to/run.json    # validate and retain an existing run
just eval-publish                    # prepare and verify local snapshots
just eval-upload-local               # exercise the importer against local Worker KV
```

Imports are idempotent. A different report with the same run ID is rejected.
Completed and stopped partial runs are archived. If the collector is killed,
its latest checkpoint is recovered into the archive on the next invocation.
The JSONL request ledger remains the source for reserved attempts and backoff.
Keep the entire data directory, including the ledger, when moving a runner.

## Scheduled production runner

`typeahead-eval-collector` runs as a separate Cloudflare Worker with no HTTP
route. Its cron checks every 15 minutes. A single Durable Object keeps the
active run and request reservations in SQLite, and schedules one alarm per
request. It uses the same sampling, corpus, quotas, backoff and snapshot
format as the Bun runner. The public Worker only reads the published snapshots
from KV.

An operator-requested full run can set a new `EVAL_MANUAL_RUN` identifier on
the collector deployment. Each identifier is consumed once in durable state.
It bypasses daily eligibility and permits up to 80 attempts per provider in
the rolling window for this run, including the preceding run. Request spacing
and upstream cooldowns still apply. Ordinary scheduled runs retain the 48-attempt
budget. Remove the variable after the requested run has completed and verified.

The latest archived run sets the initial 24-hour gate. A reservation is written
before each request. If the runner restarts with an unresolved reservation,
it archives the partial run and leaves that attempt pending. Retrying publication
or public verification does not send more search requests. The publisher logs
`eval_published` after KV readback and `eval_visible` after both the public JSON
and the default page show the run. A failed readback leaves the checkpoint
available for an alarm retry or the next cron.

On 2026-11-16, commit `05168f8` was deployed as collector version
`6d2daa2b-85dc-49d5-9af0-472a104a1001`. Cloudflare readback confirmed the
15-minute trigger and disabled public and preview subdomains. The live page
and JSON still agreed with the 80-observation seed. The first eligible cron
tick was scheduled for 07:32 UTC on 2026-11-17. A local workerd check verified
Durable Object alarm execution, 429 backoff and KV publication against a
fixture. The production run remained pending at rollout.

Deploy the collector independently after publishing the code:

```sh
bunx wrangler deploy -c deploy/eval/wrangler.jsonc --dry-run
bunx wrangler deploy -c deploy/eval/wrangler.jsonc
```

To pause collection, set `triggers.crons` to `[]` in the collector configuration
and deploy. Allow any active run to finish through its already scheduled alarms.

For self-hosting, use the same portable Bun entry points:

```sh
just eval-collect       # a finite manual run, subject to the persistent budget
just eval-tick          # one scheduled tick, with local snapshots
just eval-tick-remote   # one scheduled tick, with publication to Cloudflare KV
```

Run one publisher per KV namespace. If moving production collection to a host,
pause and drain the Cloudflare collector first and import the retained reports
before starting the host's schedule. Use one persistent data directory for both
manual and scheduled collection. After an interruption, verify the recorded
lock owner exited before removing a local lock. Keep its request ledger.

## Rollout import

After the code is published, seed production before deploying the page:

```sh
just eval-upload-remote
just deploy-worker
```

The uploader uses the existing `KV` binding in `wrangler.jsonc` and the installed
[Wrangler KV commands](https://developers.cloudflare.com/kv/reference/kv-commands/).
It writes each content-addressed `evaluation:run:<sha256>` snapshot, reads it
back, then updates `evaluation:index:v1`. It merges existing run entries rather
than replacing history with just the local files. Re-importing identical runs
makes no writes. A snapshot upload failure leaves the old index intact.

The read path loads the bounded index and one selected snapshot, validates its
checksum and schema, and renders it. Unknown run IDs return 404; corrupt or
missing indexed snapshots fail visibly. The selector lists the latest 365 runs;
older raw files and KV snapshots remain retained. Back up the data directory;
there is no automatic raw-data deletion. KV propagation can delay visibility
across locations, so check `/eval` and download the initial run after rollout.
The initial production import was verified on 2026-11-16; subsequent imports
use the same idempotent path.

## Where time goes

The collapsed timing disclosure preserves the main score view. Open
`/eval?view=timing#timing` for a duration breakdown, then select an individual
request or the whole-run arithmetic mean. The outer stack partitions elapsed
time into outside-Worker, cache lookup, backend round trip, remaining Worker
time and unattributed time. Bluesky's total uses the same scale. Nested
transport and engine intervals overlap and are shown separately, with their
own coverage counts. Missing measurements are unknown, not zero. This view
does not change scores or the immutable observations.

This is a duration chart, not a chronological waterfall: the initial archive
does not contain all interval start times. CPU flame graphs would answer a
different question; these intervals include network and asynchronous waiting.
The collector measures through the full response body. The transport's
`transport_total` covers validation and fetch through accepted response headers,
and `transport_origin_headers` covers the selected Fly fetch through headers.
`engine` preserves the engine's own `search` timer. `transport_hint_wait` and
`transport_origin_start` retain offsets for deeper analysis. Public cache hits
replace these timings with current hit timings rather than replaying the
cached request's engine cost. Future runs already archive the complete header.

For the initial run, a separate
[log-evidence file](../data/eval/timing-evidence/2026-11-16T07-25-35.153Z.json)
recovers route and Fly-fetch durations for all 40 typeahead requests from
existing `search_response_timing` events. Each event was uniquely matched by
Cloudflare request ID and verified against query, timestamp and backend
duration. Runtime joins also require the original run, observation ID and
elapsed time. No benchmark was repeated and no engine timings from separate
probes were substituted. The page links the evidence and unchanged raw run.

## Score definition

`service-v1` defaults to:

| dimension | weight | points |
| --- | ---: | --- |
| response speed | 40 | 100 at ≤100 ms, 0 at ≥1,000 ms; `100 × log10(1000 / ms)` between |
| successful answers | 40 | 100 for a valid answer, including empty results; 0 for failure |
| exact-handle rank | 20 | 100 if the requested exact handle is first; 0 otherwise |

Weights normalize to 100%; they are editable and preserved in the URL. Each
query has equal weight within its dimension, after averaging its passes.
Only completed provider pairs participate. Failed responses score zero for
speed and successful answers, and zero for an applicable exact-handle check.
Unmatched, pending and Bluesky-fallback pairs are excluded and counted visibly.
If a weighted dimension has no evidence, the aggregate is unscored instead of
silently redistributing weight. All-zero weights also produce no score.

This is an explicitly weighted service benchmark, not a general relevance
score. DID overlap, extra returned accounts and profile decoration do not earn
points. Score definitions are versioned independently from raw reports; changing
weights requires no recollection. Query filters affect only the test-set
inspection, so narrowing a drill-down does not change the headline score.

## Sampling and limits

The versioned corpus in `src/eval/model.ts` retains the eleven canonical queries
from `scripts/bench.py`, plus exact handles, multiword names, Unicode and short
prefixes. Every new report snapshots its query manifest. Twenty queries run
twice with `limit=10`: 40 requests per provider. Provider order alternates.
The endpoints are:

- `https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors`
- `https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead`

There is at least a five-second global gap after each response. The persistent
budget is 48 attempts per provider per rolling 24 hours, including failed and
unfinished attempts. A 429 or exhausted quota imposes at least one hour of
backoff, extended by `Retry-After` or `ratelimit-reset`; server and network errors
impose at least five minutes. The run stops on those signals, without retries
or catch-up bursts. Requests have a ten-second timeout and a one-MiB body limit.

Requests identify this project and send `X-Client: bench`: they count in our
operational requests but not app-adoption statistics. The public cached Bluesky
host follows its [public-client guidance](https://bsky.network/docs/rate-limits/).
Our budget is a conservative local policy, not a published upstream quota.

Timing measures fetch start through the full body, excluding JSON validation.
It includes the runner's network and connection costs and CDN behavior. A second
pass is not guaranteed warm. Cache state is reported from headers; unknown is
not a miss. Median, mean and nearest-rank p90 summaries use successful responses.
The curated sample cannot establish a latency SLO, throughput or global latency.

Agreement is DID intersection / union; two empty lists have no score. First-result
agreement uses nonempty pairs. Profile-field percentages count actor appearances,
including repeats; avatar URLs are not fetched. These endpoints do not establish
comparable corpus totals, traffic volume, moderation coverage or identity freshness.

The [November 16 latency and Seek investigation](investigations/2026-11-16-seek.md)
decomposes the initial saved run and compares the serving and index designs.
Its additional diagnostic reads are separate from the scored eval dataset.

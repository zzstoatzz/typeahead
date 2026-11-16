# Actor-search evaluation

`/eval` compares typeahead with Bluesky. The first view is a weighted benchmark
score, with dimensions, raw metrics and the test set available as drill-downs.
The website reads saved snapshots; it never starts collection.

Deployed unlisted on 2026-11-16 at [typeahead.waow.tech/eval](https://typeahead.waow.tech/eval)
from commit `2e41970`, Worker version `aaf2ce60-05f9-4e56-97b6-9397d0f6d89c`.
The initial run is imported. The live [run download](https://typeahead.waow.tech/eval/data.json?run=2026-11-16T07%3A25%3A35.153Z)
was verified equal to the retained seed, with all 80 observations. API and image
smoke checks passed; optional admin CRUD checks were not run without an admin
secret. Navigation is unchanged and the page has `noindex`. No slot, transport,
route or collection schedule was changed.

## Retained history

The initial 2026-11-16 run is in
[`data/eval/runs/2026-11-16T07-25-35.153Z.json`](../data/eval/runs/2026-11-16T07-25-35.153Z.json):
80 measured responses, 40 per provider. Its observations are unchanged from the
original local report. The location label is generalized to `local laptop`, and
the query manifest is included. It is measured data, not a generated fixture.
The seed is deliberately outside the ignored `.eval/` directory so rollout can
import it into an empty store.

New runs are retained as immutable JSON files under `.eval/runs/`. Set
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

## One collector and publisher

`scripts/eval/schedule.ts` owns one scheduled cycle: check whether a daily run
is due, collect using the existing runner, retain the raw report, and publish.
`src/eval/` owns the shared report format, scoring and snapshot protocol.
There is no evaluation work in the app's cron, stats writer, search handler or
browser. No extra database, Worker binding, or recurring service is installed.

```sh
just eval-collect       # a finite manual run, subject to the persistent budget
just eval-tick          # one scheduled tick; local publication only
just eval-tick-remote   # the same tick, then publish retained runs to production KV
```

A tick runs at most once per 24 hours from the last actual run start. Repeated
ticks skip collection; missed days are not replayed. Publication can be retried
without collecting again. Use one host and one persistent data directory for
both manual and scheduled collection. Local locks exclude overlapping collectors
and publishers; these are not distributed locks. Keep one publisher for a KV
namespace because KV does not provide a conditional manifest write.

For rollout, configure an external scheduler on that host to invoke the command
hourly (absolute paths to the checkout and Bun are required in cron):

```cron
17 * * * * cd /path/to/typeahead && EVAL_DATA_DIR=/var/lib/typeahead-eval EVAL_LOCATION=runner-region /path/to/bun scripts/eval/schedule.ts --remote >> /var/log/typeahead-eval.log 2>&1
```

The daily gate and request budget live in the runner, not in cron. This requires
Bun, the installed project dependencies, writable persistent storage and Wrangler
authentication for publication. The current adapter is a standalone host command,
not a Cloudflare Cron Trigger: a future Cloudflare adapter should call the same
collector/state contract rather than introduce a second collection implementation.
No recurring schedule is active as part of this local work.

Locks contain owner PIDs. On an interruption, verify that process has exited
before removing its lock directory. Do not reset the ledger to clear a lock.
Normal exits clean up locks; incomplete attempts still consume the budget.

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

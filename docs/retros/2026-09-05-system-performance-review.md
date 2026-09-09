# Typeahead performance, resources, and cost — September 5

The ingester loss mechanism is mitigated and live ingestion is current. Finish
and verify the bounded account recovery, then keep general profile cleanup
separate. This review does not require another production deployment.

## What the measurements establish

- Current Fly search HTTP P95 is approximately 23 ms; recent P99 is 40–46 ms.
  These are origin HTTP measurements, not browser-to-Worker latency and not
  proof that every search returned useful results.
- Both Fly services currently have low CPU execution and no throttling.
  The earlier ingester CPU chart included steal time; execution, steal, and
  quota throttling need distinct interpretation.
- Search has 4 shared CPUs, 2 GiB RAM, and a 60 GB volume, with approximately
  24.7 GiB filesystem space available. Its health endpoint reports roughly
  1.36 GiB RSS. The dashboard's approximately 23% memory pressure is based on
  available memory and includes reclaimability; it is not process RSS.
- There is only one search machine. Low utilization does not establish safe
  downsizing or availability during restart.
- Bulk enrichment shared the Turso writer with ingestion and caused significant
  ingestion lag. Pausing it reduced write latency and cleared the lag. Its daily
  schedule is paused and there are no running/pending/scheduled bulk runs as of
  this review. The local yielding guard is tested but not published/deployed.
- A diagnostic search for `bs` read 332,783 overlay entries for one prefix and
  took about four seconds. A bounded lookup fix passes local tests but is not
  deployed. This explains that query, not every observed latency outlier.

## Request accounting also consumes the shared writer

The current code issues two counter statements for a normal edge-cache hit:
`recordCacheHit`, then the outer `recordRequest`. A successful normal origin
search issues four: `recordMetric`, the two `recordTrafficSource` statements,
and `recordRequest`. These run with `waitUntil` after the response, but still
consume database work and write budget. These are logical SQL statements, not
an assertion about billable rows or physical writes.

This is a useful first candidate for reducing shared-writer traffic. Measure its
actual contribution before choosing batching, sampling, or a different metrics
store. Preserve distinctions between total requests, cache misses, successful
responses, empty answers, and transport errors.

## Verified costs so far

Fly Cost Explorer, selected August 30–September 5, 2026:

| App / item | Reported spend |
| --- | ---: |
| typeahead-search, total | $6.42 |
| — volumes | $2.66 |
| — volume snapshots | $0.81 |
| — EWR resources | $2.93 |
| typeahead-ingester, total | $0.55 |
| Both apps | $6.97 |

Small differences between components and totals reflect rounding and other
regions. This is the dashboard's reported period spend, not a monthly quote or
steady-state forecast. The period includes architecture changes. The whole
organization's $39.98 is not Typeahead's cost.

Source: https://fly.io/dashboard/nate-nowack/billing/cost-explorer?end_date=2026-09-05&start_date=2026-08-30&view=breakdown

The Turso CLI is not authenticated; current plan/usage/invoice amounts remain
unverified. Cloudflare/R2, Logfire, and the share of home build resources also
remain to be attributed. `docs/cost-scalability.md` contains historical estimates
and should not be used as the current total.

## Discussion order

1. User-visible performance: Worker latency, cache hit rate, timeouts and empty
   results, including the slowest searches hidden by P95.
2. Resource contention: ingestion versus enrichment and metrics writes;
   snapshot/overlay growth, sync I/O, available memory and CPU quota behavior.
3. Cost: current vendor usage and invoices, storage/snapshot retention, and
   maintenance work. Attribute shared plans explicitly.
4. Capacity and reliability: what a larger traffic burst would stress, and how
   to test changes safely with a single production search machine.

Do not resize production on the strength of a quiet CPU graph. Establish the
working set and tail-latency behavior first.

At approximately 23:06 UTC, recovery's P99 gate rose to 270.5 ms and stopped
further recovery writes automatically; P95 remained approximately 23 ms with
zero observed HTTP errors. Search's 23:06 incremental sync took 14 seconds
(three apply spans around 3.9, 6.1, and 2.8 seconds). This is a timing
correlation, not proof of causation. Ingestion remained current and search
freshness healthy. Leave the eight remaining restoration candidates paused
until the gate clears; do not loosen the gate to finish the incident.

Recovery subsequently completed at approximately 23:23 UTC: all 377 restored
accounts were verified through search and no recovery writer remains running.
The final eight and identity correction were applied only after the unchanged
health gate cleared. See `2026-09-05-ingester-recovery-closeout.md`; historical
field-level uncertainty is retained explicitly rather than expanded into
unbounded data cleanup.

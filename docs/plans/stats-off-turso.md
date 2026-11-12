# Request and traffic statistics off Turso

**Status, 2026-11-12:** steps 1 and 2 of the migration sequence (schema,
binding, adapter, dual write) have been live since 2026-11-10; see
[operations](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operations.md#statistics-dual-write-to-d1).
Steps 3 to 6 (backfill, comparison, read switch) are implemented and tested
locally on the `stats-d1-read` branch, not deployed and not run; the operator
sequence is in
[operations](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operations.md#statistics-read-cutover-to-d1)
and the differences from the sequence below are under "Corrections from
implementing steps 3 to 6". Steps 7 and 8 are still a proposal. The plan
answers [issue 34](https://tangled.org/zzstoatzz.io/typeahead/issues/34) and
was written against `main` at `16801e3`.

**Recommendation:** move all nine bookkeeping tables to a dedicated Cloudflare
D1 database bound to the public Worker, keep the hourly KV materialization
exactly as it is, and cut over behind a dual-write period that compares the KV
blobs the two stores produce. Workers Analytics Engine cannot hold this data on
its own (three-month retention, sampled counts), a Durable Object does the same
job as D1 with more code, and KV alone loses counts.

How to read this document: **measured** means read from the source tree or from
one fetch of the public `/stats` page on 2026-11-10 at 18:12 UTC. **Documented**
means quoted from Cloudflare's documentation, fetched the same day. **Estimate**
means arithmetic on those two. Anything else is marked unverified.

## What writes and reads the statistics today

### Writers

| Writer | When it runs | Tables | Source |
| --- | --- | --- | --- |
| `recordRequest` | every non-OPTIONS answer, in `ctx.waitUntil` | `request_buckets` | `src/index.ts:122-127`, `src/metrics.ts:158-172` |
| `recordMetric` | every search that is not a cache hit, including empty terms and Bluesky fallback | `metrics` (`searches`, `total_ms`) | `src/handlers/search.ts:132,138,399`, `src/metrics.ts:4-15` |
| `recordCacheHit` | every search answered from the edge cache | `metrics` (`cache_hits`, `cache_ms`) | `src/handlers/search.ts:160`, `src/metrics.ts:17-28` |
| `recordTrafficSource` | every search cache miss that produced an answer | `traffic_sources`, `traffic_buckets` (one two-statement batch) | `src/handlers/search.ts:139,225,388`, `src/metrics.ts:95-110` |
| `recordActorDelta` | ingest batches with new actors, admin deletes, cron dead-actor cleanup | `actor_deltas`, `counters.actors_total` | `src/handlers/ingest.ts:122`, `src/handlers/admin.ts:69`, `src/cron.ts:276`, `src/metrics.ts:33-57` |
| `recordSnapshot` | hourly cron | `snapshots` | `src/index.ts:67`, `src/enrichment.ts:16-45` |
| `materializeStats`, `materializeRequests` | hourly cron | reads all of the above, writes KV `stats_data` and `request_data`, rolls buckets older than 90 days into `traffic_daily` and `request_daily`, deletes them | `src/index.ts:72`, `src/enrichment.ts:48-183` |
| `recordProfile` | hourly cron | reads 28 days of `traffic_buckets` and `request_buckets`, writes KV `stats_profile` | `src/index.ts:73`, `src/stats-profile.ts:66-85` |
| `recordExclusions` | hourly cron, recounts once per 23 hours | reads `actors` and `mod_overrides`, writes KV `search_exclusions` | `src/index.ts:68-71`, `src/exclusions.ts:57-103` |

A search therefore costs two or three separate Turso write round trips (the
request row, the metrics row, and on a miss the two traffic rows). Every other
answer costs one.

Three details the source shows that the issue does not:

- `handles_delta` and `avatars_delta` are never written with a non-zero value.
  All three `recordActorDelta` call sites pass only `actors`. The stitching in
  `src/handlers/stats.ts:83-100` adds zeros for those two fields.
- No consumer reads the `domain` column of `request_buckets` or
  `request_daily`. The page sums it away in `sliceRequests`
  (`src/pages/stats.ts:231-259`) and the profile sums whole buckets.
- The comment at `src/handlers/stats.ts:38` says `/stats` "never hits turso".
  It does: one seven-statement read batch at lines 55-59 on every edge-cache
  miss (the page is edge-cached for 60 seconds per normalized URL).

The DDL for `metrics`, `snapshots`, `actor_deltas`, `traffic_sources` and
`counters` is in `services/src/db/migrations.zig:110-136,195-200`.
`traffic_daily` and `request_daily` are created by the cron
(`src/enrichment.ts:61-65,138-143`). The DDL for `traffic_buckets` and
`request_buckets` is not in the repository; their primary keys are implied by
the upserts (`(bucket, domain)` and `(bucket, domain, endpoint, status)`).

### Readers

| Reader | What it reads | Source |
| --- | --- | --- |
| `/stats` handler | KV `stats_data`, `request_data`, `stats_profile`, `search_exclusions`; Turso `traffic_sources` (list and totals) and the live tail of `metrics`, `traffic_buckets`, `request_buckets` | `src/handlers/stats.ts:39-59`, `src/stats-tail.ts:10-34` |
| `/stats` page | renders the above | `src/pages/stats.ts` |
| Preview image | KV `stats_data` only (`traffic_series`, `snapshots`, `metrics`, `traffic_sources_total`) | `src/handlers/og.ts:166-178,225`, `src/image-renderer.ts`, `deploy/image-renderer/wrangler.jsonc` |
| `/source-icon` | `SELECT 1 FROM traffic_sources WHERE domain = ?` when the source is not a known brand | `src/handlers/source-icon.ts:43-46` |
| Smoke suite | `/stats` returns 200 HTML containing "actors indexed", "searches / 5 min", "hidden by policy", "invalid identity" | `scripts/smoke.py:255-275` |

The public Worker binds KV, two rate limiters and two service bindings
(`wrangler.jsonc`). It has no Analytics Engine, D1 or Durable Object binding.
The search transport Worker has a SQLite-backed Durable Object
(`deploy/search-transport/wrangler.jsonc`).

## What each consumer needs

| # | Datum | Current source | Granularity | Window | Dimensions | Consumer |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Search count, cache-hit count, summed latency for each | `metrics` → KV `stats_data.metrics` plus live tail | 5 minutes, reduced server-side to totals per range | 1h, 24h, 7d, 30d, all time, plus the preceding window of equal length for the change badge (so 60 days at 5-minute grain, all time as a sum) | none | `/stats` activity tiles: searches, avg cached, avg cold (`src/pages/stats.ts:129-153`) |
| 2 | Searches answered, all time | sum of every `metrics` row in KV | scalar | all time | none | preview image overview tile (`src/handlers/og.ts:168`) |
| 3 | Cache-miss searches per app, 5-minute | `traffic_buckets` → KV `traffic_series_5m` plus live tail | 5 minutes | last 48 hours | app | `/stats` per-app chart, 1h and 24h ranges |
| 4 | Cache-miss searches per app, hourly | `traffic_buckets` → KV `traffic_series` plus live tail | hour | last 30 days | app | `/stats` per-app chart, 7d and 30d; link-preview title and description; preview image comparison bars |
| 5 | Cache-miss searches per app, daily | `traffic_daily` ∪ `traffic_buckets` → KV `traffic_daily` | day | all time (first row 2026-07-24, measured) | app | `/stats` per-app chart, "all" range; fallback app count on the preview image |
| 6 | Cache-miss searches per app, cumulative | `traffic_sources`, read live | scalar per app | all time | app | `/stats` app list, pie, "N requests · M apps" line |
| 7 | Count of apps that ever searched | `traffic_sources` with the probe filter → KV `traffic_sources_total` | scalar | all time | none | preview image "apps" tile |
| 8 | Is this a known source | `traffic_sources` point read | boolean | all time | app | `/source-icon` |
| 9 | Answers by endpoint | `request_buckets` / `request_daily` → KV `request_data` plus live tail | 5 minutes for 48 hours, hourly for 30 days, daily for all time (first row 2026-08-23, measured) | 1h to all | endpoint (13 fixed labels) | `/stats` endpoint view |
| 10 | Answers by status class | same rows as 9 | same | same | status class (`2xx`…`5xx`, derived from the stored code) | `/stats` status view |
| 11 | Typical range per UTC hour of day | 28 days of 5-minute totals from `traffic_buckets` and `request_buckets`, ending one day back → KV `stats_profile` | 24 bands at 5-minute grain and 24 at hourly grain | 28 days | none (whole-bucket totals) | shaded band behind the total line on `/stats` |
| 12 | Actor totals: indexed, with handles, with avatars, hidden | `snapshots` → KV `stats_data.snapshots` | hour | all time (first point 2026-03-17, measured) | none | `/stats` trend chart and coverage tiles; preview image "actors indexed" and "with avatars" tiles |
| 13 | Actor count since the last snapshot | `actor_deltas` → KV `stats_data.deltas` | 5 minutes | last 24 hours | none | points stitched after the last snapshot on the trend chart |
| 14 | Running actor total | `counters.actors_total` | scalar | current | none | input to row 12 |
| 15 | Exclusion counts | `actors`, `mod_overrides` → KV `search_exclusions` | scalar | current, daily | none | `/stats` "hidden by policy" and "invalid identity" |

Rows 3, 9, 10 and the tail of row 1 are what the last-hour view depends on:
the page replaces every bucket from the top of the hour of the last KV write
with rows read live (`src/stats-tail.ts:10-13`), at most two hours of
5-minute buckets.

## Current volume

Measured from the public page, 2026-11-10 18:12 UTC:

| Quantity | Value |
| --- | --- |
| Answers recorded, last 48 hours | 270,869 (about 135,000 a day) |
| Answers per full day, 2026-11-01 to 11-09 | 111,283 to 140,943, except 49,515 and 47,276 on 11-06 and 11-07 |
| Busiest 5-minute bucket in 48 hours | 1,194 answers (about 4 a second); median bucket 423 |
| Share of answers from the ingester's `/admin/*` calls, last 48 hours | 225,539 of 270,869 (83%): `admin/identity` 186,755, `admin/ingest` 24,150, `admin/delete` 9,682, `admin/labels` 4,952 |
| Search answers, last 48 hours | 34,608 |
| Searches, last 24 hours | 17,596 (17,100 uncached, 496 cache hits) |
| Searches, last 7 days | 114,936 |
| Cache-miss searches attributed to an app, per full day 11-01 to 11-09 | 14,071 to 16,862 |
| Answers recorded since 2026-08-23 | 5,284,074 |
| Searches recorded, all time | 1,218,614 |
| Apps in `traffic_sources` after the probe filter | 388, summing 1,136,828 hits |
| Distinct apps with traffic: last 48 hours / 30 days / daily history | 74 / 243 / 357 |
| Apps per bucket: 5-minute / hourly / daily (median, maximum) | 2, 8 / 9, 20 / 39, 65 |
| Endpoint labels with traffic | 13 (the whole fixed set) |
| Status classes with traffic | 4 |

Not measurable from public data: the number of distinct
`(domain, endpoint, status)` series and the row counts of the bucket tables.
Estimate: about 15 distinct rows per 5-minute request bucket, so roughly 4,300
request rows a day and under 400,000 in the 90-day table.

Estimated write statements to Turso from statistics: 135,000 request rows,
17,600 metrics rows, 31,000 traffic rows and up to 24,000 actor-delta rows a
day, about 208,000 a day or 6.2 million a month. Each is its own HTTP round
trip. The last committed plan-level figure is 45.2 million rows written across
the whole database in one billing period
([Turso usage analytics](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/turso-usage-analytics.md));
the two are not from the same period and should not be divided.

"Ten times" below means 1.35 million answers a day. Whether volume goes that
way depends mostly on the ingester: if its `/admin/*` calls stop passing through
the Worker when the corpus leaves Turso, recorded answers fall to roughly
23,000 a day.

## Options

All prices are Workers Paid, documented on 2026-11-10. The $5 monthly minimum
is already paid by the account according to
[COSTS.md](https://tangled.org/zzstoatzz.io/typeahead/blob/main/COSTS.md)
(dated 2026-06-17; not re-verified).

### Workers Analytics Engine

Documented
([pricing](https://developers.cloudflare.com/analytics/analytics-engine/pricing/),
[limits](https://developers.cloudflare.com/analytics/analytics-engine/limits/),
[sampling](https://developers.cloudflare.com/analytics/analytics-engine/sampling/),
[SQL API](https://developers.cloudflare.com/analytics/analytics-engine/sql-api/),
[get started](https://developers.cloudflare.com/analytics/analytics-engine/get-started/)):

- "10 million included per month (+$0.25 per additional million)" data points;
  "1 million included per month (+$1.00 per additional million)" read queries;
  "Currently, you will not be billed for your use of Workers Analytics Engine."
- "up to twenty blobs, twenty doubles, and one index per call"; "a maximum of
  250 data points per Worker invocation".
- "Data written to Workers Analytics Engine is stored for three months."
- "You do not need to await `writeDataPoint()`"; it "will return immediately".
- "At write time, we sample if data points are written too quickly into one
  index." "For relatively uncommon index values, we may write all of the data
  points". "There is no guarantee you can retrieve any one individual record."
  Queries over longer ranges "retrieve data from a higher sample interval".
- Reads go through `https://api.cloudflare.com/client/v4/accounts/<account_id>/analytics_engine/sql`
  with a bearer token holding Account Analytics Read.

Assessment:

- Write path is the best of the four: no round trip, cannot delay or fail a
  response.
- Rows 2, 5, 6, 7 and the "all" ranges of 1, 9, 10 need history beyond three
  months, and row 8 needs a durable set. Analytics Engine cannot hold them, so
  a second store with its own rollup job is required anyway.
- Counts are estimates (`sum(_sample_interval)`). The documentation gives no
  threshold below which data is unsampled, so a dual-write comparison cannot
  demand equality and "the same numbers" becomes "close numbers".
- The live tail would be an HTTPS call to the Cloudflare API from `/stats`
  with a new account-scoped secret. The documentation does not state how soon
  a written point becomes queryable; unverified.
- Cost: one data point per answer is about 4.1 million a month, inside the
  included 10 million: $0. At ten times, about 40.6 million: $7.65 a month if
  billing is enforced.
- Retires three tables by itself (`metrics`, `traffic_buckets`,
  `request_buckets`). The other six still need a home.
- History: no import path. Clean break for 5-minute and hourly data.

### SQLite-backed Durable Object

Documented
([pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/),
[limits](https://developers.cloudflare.com/durable-objects/platform/limits/)):

- Requests "1 million / month, + $0.15/million"; "Each RPC session counts as
  one request"; alarms count as requests.
- Duration "400,000 GB-s / month, + $12.50/million GB-s"; an idle object
  eligible for hibernation is not billed.
- SQLite rows written "First 50 million / month included + $1.00 / million
  rows"; rows read "First 25 billion / month included + $0.001 / million
  rows"; stored data "5 GB-month, + $0.20/ GB-month". The page says storage
  billing "will be enabled in January 2026"; whether it is active now is not
  stated.
- "10 GB" per object; "An individual Object has a soft limit of 1,000 requests
  per second."

Assessment:

- Fits every row: it is SQLite, so the bucket tables, the rollups and the tail
  queries carry over. Reads are strongly consistent, so the last-hour tail and
  the status view are exact.
- Needs a new class, a migration tag, and an RPC method for every read the
  cron, `/stats` and `/source-icon` make. No ad hoc SQL from a laptop without
  building an admin method.
- Failure: the call sits in `ctx.waitUntil` after the response, same as today.
- Cost: one RPC per answer is about 4.1 million requests a month: $0.46.
  About 6.2 million rows written: included. At ten times: $5.94 for requests
  plus about $12 for rows, about $18 a month. Batching increments inside an
  isolate would cut the request count but loses whatever an evicted isolate
  had not flushed.
- Retires all nine tables as storage.
- History: importable through a batch RPC, since the schema is unchanged.

### D1

Documented
([pricing](https://developers.cloudflare.com/d1/platform/pricing/),
[limits](https://developers.cloudflare.com/d1/platform/limits/),
[import and export](https://developers.cloudflare.com/d1/best-practices/import-export-data/),
[batch](https://developers.cloudflare.com/d1/worker-api/d1-database/)):

- Rows written "First 50 million / month included + $1.00 / million rows";
  rows read "First 25 billion / month included + $0.001 / million rows";
  storage "First 5 GB included + $0.75 / GB-mo". "Indexes will add an
  additional written row when writes include the indexed column".
- "10 GB" per database; "1000" queries per Worker invocation; "30 days" Time
  Travel.
- "Each individual D1 database is inherently single-threaded"; "If your
  average query takes 1 ms, you can run at most 1,000 queries per second";
  when the queue is full "the database will return an "overloaded" error."
- "Batched statements are SQL transactions"; a failing statement "aborts or
  rolls back the entire sequence."
- Import with `wrangler d1 execute <db> --remote --file=<dump.sql>` after
  removing `BEGIN TRANSACTION` and `COMMIT`.

Assessment:

- Fits every row for the same reason the Durable Object does, with less code:
  the repository's `TursoDB` interface (`src/db.ts:7-18`) is already the D1
  statement shape (`prepare`, `bind`, `all`, `first`, `run`, `batch`), so the
  record and materialize functions take a D1-backed handle unchanged.
- The last-hour tail, the 28-day profile and the status view are the same SQL
  against the same schema. Exact counts, no sampling.
- A single writer, like Turso, but it is no longer shared with ingest. Peak
  measured load is about 4 writes a second.
- Failure: writes stay in `ctx.waitUntil` with the error swallowed. A D1
  outage or an "overloaded" error loses that increment, as a Turso error does
  today, and cannot reach the response.
- Cost: about 6.2 million rows written a month: $0. Estimated cron and tail
  reads, well under one billion rows a month: $0. Storage well under 5 GB: $0.
  At ten times: about 62 million rows written, about $12 a month.
- Retires all nine tables as storage.
- History: full carry-over by SQL dump, including daily rollups back to
  2026-07-24 and snapshots back to 2026-03-17.
- Ad hoc queries from a laptop (`wrangler d1 execute --remote`) keep the
  "check `/stats?view=status` first" habit workable below the page.

### In-isolate batching to KV only

Documented
([pricing](https://developers.cloudflare.com/kv/platform/pricing/),
[limits](https://developers.cloudflare.com/kv/platform/limits/),
[how KV works](https://developers.cloudflare.com/kv/concepts/how-kv-works/)):

- Writes "1 million/month, + $5.00/million"; list requests "1 million/month,
  + $5.00/million"; reads "10 million/month, + $0.50/million".
- "Writes to same key | 1 per second"; value size "25 MiB".
- "Changes may take up to 60 seconds or more to be visible in other global
  network locations". For "updating the same key tens or hundreds of times per
  second, KV will not be an ideal fit"; "KV is not ideal for applications
  where you need support for atomic operations".

Assessment:

- KV has no increment. Isolates in different locations would overwrite each
  other, so each isolate must write its own shard key and the cron must list
  and merge them. An isolate that is evicted before its next request loses its
  unflushed counts, and nothing reports the loss.
- The last-hour tail lags by the flush interval plus propagation and
  undercounts by whatever was lost. The per-status view loses exactly the
  rare events it exists to show.
- Cost: unbatched, about 4.1 million writes a month is $15.30, and $198 at ten
  times. Batched cost depends on the number of live isolates, which is not
  known.
- Could represent six tables. Not recommended for any of them.

### Comparison

| | Analytics Engine | Durable Object | D1 | KV only |
| --- | --- | --- | --- | --- |
| Rows of the needs table it can serve alone | 3, 4, 11, and the ranges up to 30 days of 1, 9 and 10 | 1 to 14 | 1 to 14 | none exactly |
| Last-hour tail | works, freshness unverified, sampled | exact | exact | lagging, lossy |
| Per-status view | works, sampled | exact | exact | lossy |
| Counts | estimated | exact | exact | undercount |
| Read path for `/stats` | KV plus Cloudflare API call with a new secret | KV plus RPC | KV plus D1 batch | KV |
| Can delay or fail a search response | no | no | no | no |
| Monthly cost now | $0 | $0.46 | $0 | $15.30 unbatched |
| Monthly cost at ten times | $7.65 | about $18 | about $12 | $198 unbatched |
| History | clean break | carried over | carried over | clean break |
| Tables retired from Turso | 3 | 9 | 9 | 6, with loss |
| New code | binding, rollup job, second store | class, RPC surface, migration tag | binding, adapter, schema file | shard and merge protocol |

Rows 12 to 15 are stored by any option that has tables, but their inputs come
from the actor corpus; see the next section.

## What still depends on the actor corpus

These numbers are computed from `actors` in Turso and will need a new source
when the corpus moves to an embedded store (issue 31). Moving the statistics
tables does not change that.

| Number | Where it comes from now | Shown on |
| --- | --- | --- |
| `actors_total` | `changes === 1` on the `INSERT OR IGNORE INTO actors` probes in `/admin/ingest` (`src/handlers/ingest.ts:37-44,117-123`), minus deletes in `src/handlers/admin.ts:69` and `src/cron.ts:276` | "actors indexed" on `/stats` and the preview image |
| `hidden` | `SELECT COUNT(*) FROM actors WHERE hidden != 0` (`src/enrichment.ts:20`) | subtracted from the total |
| `with_handles` | total minus `COUNT(*) WHERE handle = '' COLLATE NOCASE` (`src/enrichment.ts:27`) | handle coverage, trend chart, preview image |
| `with_avatars` | total minus `COUNT(*) WHERE avatar_url = ''` (`src/enrichment.ts:28`) | avatar coverage, trend chart, preview image |
| `policy_hidden`, `stale_hidden_flags` | every `actors` row with `hidden != 0`, re-judged with `mod_overrides` and blocked domains (`src/exclusions.ts:61-89`) | "hidden by policy" |
| `handle_invalid`, `quarantined` | two counts on `idx_actors_handle` (`src/exclusions.ts:39-41`) | "invalid identity" |

The request, search and traffic counters (rows 1 to 11) do not depend on the
corpus at all.

Until the corpus moves, the cron keeps running these counts against Turso as
reads and stores the result in D1. After it moves, whoever owns the embedded
store has to publish the same six numbers on a schedule; the Worker should
accept them rather than compute them. That interface is an open question
below, not part of this plan.

## Recommended design

1. One D1 database, `typeahead-stats`, bound to the public Worker as
   `STATS_DB`. Schema: the nine tables with their current definitions, taken
   from a schema dump of production so that `traffic_buckets` and
   `request_buckets` match what exists rather than what the upserts imply.
2. A `d1Db(binding)` adapter beside `tursoDb` in `src/db.ts`, both returning
   the same interface. The interface is renamed from `TursoDB` to a neutral
   name in a mechanical commit of its own.
3. Every record function, the cron's snapshot write, materialization, rollup
   and profile, the `/stats` tail and source list, and the `/source-icon`
   lookup take the statistics handle instead of the corpus handle.
4. KV keys, their shapes, the hourly schedule, the image renderer and the page
   are untouched. The image renderer keeps reading KV only.
5. The three writes a search makes are left as three statements in this
   change. Folding them into one `batch` per response is a follow-up; it
   changes failure grouping and is not needed at 4 writes a second.

Not in scope: dropping the unread `domain` column from the request tables,
dropping the two always-zero delta columns, or replacing KV materialization
with direct D1 reads. Each changes what is stored or shown and would break the
comparison below.

## Migration sequence

Each step is one deploy and leaves `/stats` and the preview image reading a
single, complete source.

1. **Schema and binding.** Create the database, apply the schema, add the
   binding and the adapter. Nothing writes or reads it. `/stats` unchanged.
2. **Dual write.** Every writer in the first table writes to Turso and to D1
   independently, each in its own `waitUntil` with its own swallowed error.
   Turso remains the only source for reads. Record `T0`, the first 5-minute
   bucket that begins after the deploy is live everywhere.
3. **Backfill.** From one Turso read transaction, export `metrics`,
   `traffic_buckets`, `request_buckets` and `actor_deltas` rows older than
   `T0`, all of `traffic_daily`, `request_daily` and `snapshots`, and import
   them to D1. The two cumulative values cannot be copied as they are, because
   D1 has been counting since step 2:
   - `traffic_sources.hits` in D1 becomes the Turso value minus Turso's
     `traffic_buckets` sum for that app from `T0` on, plus D1's own sum from
     `T0` on. Both Turso terms come from the same read transaction.
   - `counters.actors_total` in D1 becomes the Turso value minus Turso's
     `actor_deltas.actors_delta` sum from `T0` on, plus D1's own.
   Run it between two cron runs so no rollup lands mid-export.
4. **Shadow materialization.** The cron runs materialization and the profile a
   second time against D1 and writes `stats_data_d1`, `request_data_d1` and
   `stats_profile_d1`. The rollup and prune run on each store over its own
   rows.
5. **Comparison.** A script reads both sets of KV keys and reports, per
   series and per closed bucket, any difference; it also compares
   `overviewFigures` for both blobs, which is every number on the default
   preview image. Expected result: identical before `T0`; from `T0` on,
   identical except where one store dropped a write the other kept. The
   owner sets the acceptable difference before looking at the report. The
   window must include at least one daily exclusions run, one day boundary
   and one cron run that was slow or skipped.
6. **Read cutover.** One variable switches the cron, the `/stats` tail and
   source list, and `/source-icon` to D1, and the cron writes the real KV
   keys from D1. Dual write continues, so rollback is the same variable. The
   numbers on `/stats` move by exactly the differences the comparison
   reported.
7. **Stop writing Turso.** Remove the Turso statistics writes and reads and
   the shadow keys. The cron still reads the four corpus counts from Turso.
   After this deploy no request-path code opens a Turso connection for
   bookkeeping; search, ingest and admin handlers still do until issue 31.
8. **Drop the Turso tables.** A production write on Turso: export a final
   dump first, and do it only on the owner's word.

## Corrections from implementing steps 1 and 2

- The DDL of `traffic_buckets` and `request_buckets` is now known from the
  production schema dump (`docs/ops/turso-schema-20261110.sql`). `metrics` and
  `snapshots` each carry a column added after their original migration
  (`cache_hits`/`cache_ms`, `hidden`), which the dump includes.
- `counters` needs its `actors_total` row before dual write starts:
  `recordActorDelta` increments it with an `UPDATE`, which does nothing on an
  empty table. The D1 migration seeds it at 0, so in D1 it equals the sum of
  D1's own `actor_deltas.actors_delta` and the step 3 arithmetic holds.
- The record functions each read the clock to choose a bucket. Two stores
  written from two clock readings could disagree about a row neither dropped,
  so each event now carries one reading to both stores.
- `src/handlers/search.ts` did not swallow its statistics failures; only the
  request wrapper did. Both stores' failures are now swallowed and logged as
  `stats_write_failed`, which also answers part of the "dropped writes" risk:
  the drop rate of each store is visible from step 2 on.
- The cron's dead-actor cleanup has no request context, so its two writes are
  awaited rather than handed to `waitUntil`; the same holds for the hourly
  snapshot. A Turso failure there still fails the caller as before.
- `TursoDB` could not be renamed in `src/moderation.ts`: that file's hash is
  embedded in the Tantivy engine's policy artifact. It imports an alias.
- D1's rollup and prune do not need to start with dual write. D1 starts
  empty, the backfill brings at most 90 days of buckets, and the daily series
  is computed from the daily table plus all remaining buckets, so unpruned
  buckets change nothing that is read. Step 4 remains the first place it runs.

## Corrections from implementing steps 3 to 6

- **Row counts, measured 2026-11-12.** About 294,000 statistics rows in
  Turso: `request_buckets` 141,079, `actor_deltas` 62,652, `traffic_buckets`
  49,813, `metrics` 35,153, `snapshots` 4,847, `traffic_daily` 659,
  `traffic_sources` 410. `request_daily` is empty: the oldest request bucket
  is from 2026-08-23, so the first one turns 90 days old on 2026-11-21 and
  the "daily since 2026-08-23" history is still held as buckets.
  `actor_deltas` and `metrics` are never pruned.
- **Where the backfill runs.** Not an export and import from a laptop: an
  authenticated admin endpoint copies one bounded page a call from Turso to
  D1 inside the Worker, and a command sends it positions. That is too many
  rows to pull across a home connection and few enough for a few hundred
  Worker calls.
- **The boundary is a cut, not `T0`.** Step 3 kept D1's own rows from `T0`.
  The backfill takes any closed bucket at or after `T0` and replaces D1's
  rows before it with Turso's, so D1 can be made identical to Turso up to a
  few minutes before the read switch, and the page does not move by the
  writes D1 dropped since `T0`. The offset arithmetic is the plan's with the
  cut in place of `T0`.
- **No quiet window between cron runs is needed.** The daily tables are
  copied last, read in one Turso transaction with the oldest remaining
  bucket, and written in one D1 transaction that also removes the D1 buckets
  Turso has rolled up since they were copied.
- **Rows are bound, not dumped.** A page is written as multi-row `INSERT`s of
  bound values, 100 parameters a statement, so `total_ms` and `cache_ms`
  arrive as the same doubles.
- **Steps 4 and 5 are one on-demand check.** There are no `_d1` KV keys and
  no second materialization in the cron. The parity endpoint reads what the
  cron would write from each store at one instant and compares it, along
  with the tables themselves. D1's rollup and prune first run when the cron
  reads D1, at step 6; until then its daily tables are Turso's copy and its
  old buckets are left in place, which gives the same per-day sums.
- **Only closed buckets are compared.** A bucket being written can differ
  between two stores for a moment. Hourly and daily KV rows that include an
  open bucket are counted apart.
- **The two stores roll up on different schedules** once D1 is read, so the
  comparison is of per-day sums (daily table plus remaining buckets) and of
  buckets both stores still hold, not of the daily tables row for row.
- **Reading D1 keeps D1 written.** `STATS_READ_SOURCE=d1` implies the D1
  write even with `STATS_DUAL_WRITE` off.
- **`counters` is shared.** Turso's `counters` table also holds
  `label_stream_seq`, the ingester's label cursor. It is not copied to D1 and
  it has to be dealt with before step 8 drops the table.
- **The snapshot's total stays on Turso.** `countSnapshot` reads
  `counters.actors_total` in the same Turso batch as its three `actors`
  counts, and stays there with them. D1's dual-written `actors_total` is
  backfilled and compared but nothing reads it yet.
- **Measured drop rates.** Over the 445 closed buckets from `T0` to
  2026-11-12 08:25 UTC: 183,963 answers in Turso and 183,944 in D1; 29,690
  and 29,692 attributed searches; `metrics` identical; actor deltas 29,948
  and 29,936. This partly answers "Dropped writes become visible" under
  risks: both stores drop, at about one in ten thousand.

## Files that change

Source:

- [wrangler.jsonc](https://tangled.org/zzstoatzz.io/typeahead/blob/main/wrangler.jsonc): `d1_databases` binding.
- [src/types.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/types.ts) and [src/public-worker-env.d.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/public-worker-env.d.ts): the binding and the cutover variable on `Env`.
- [src/db.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/db.ts): adapter, interface rename.
- [src/metrics.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/metrics.ts): record functions take the statistics handle.
- [src/index.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/index.ts): request wrapper and cron step wiring.
- [src/handlers/search.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/handlers/search.ts), [src/handlers/ingest.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/handlers/ingest.ts), [src/handlers/admin.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/handlers/admin.ts), [src/cron.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/cron.ts): call sites of the record functions.
- [src/enrichment.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/enrichment.ts): snapshot reads the corpus handle and writes the statistics handle; materialization and rollups use the statistics handle; the `CREATE TABLE IF NOT EXISTS` statements move to the schema file.
- [src/stats-profile.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/stats-profile.ts), [src/stats-tail.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/stats-tail.ts), [src/handlers/stats.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/handlers/stats.ts), [src/handlers/source-icon.ts](https://tangled.org/zzstoatzz.io/typeahead/blob/main/src/handlers/source-icon.ts): read from the statistics handle.
- A new schema file for D1 and a new comparison script under `scripts/`.
- [services/src/db/migrations.zig](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/src/db/migrations.zig): the statistics tables leave the Turso migrations only at step 8.

Unchanged: `src/pages/stats.ts`, `src/handlers/og.ts`, `src/image-renderer.ts`,
`src/exclusions.ts`, and the image renderer and search transport
configurations.

Documentation, in the same change as the behavior
([AGENTS.md](https://tangled.org/zzstoatzz.io/typeahead/blob/main/AGENTS.md)
requires it): the traffic-stats bullet and the Turso single-writer bullet in
`AGENTS.md`, the architecture and development sections of
[README.md](https://tangled.org/zzstoatzz.io/typeahead/blob/main/README.md),
[docs/architecture.md](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/architecture.md)
and the statistics paragraphs of
[docs/operations.md](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/operations.md).

## Tests

Change:

- `src/index.test.ts`: it mocks the libSQL client and asserts on
  `INSERT INTO request_buckets`. It needs a fake D1 binding on `Env`, and
  during dual write it asserts that both stores receive the row. The test
  "an unreachable engine returns unavailable without touching Turso" keeps
  its meaning.
- `src/metrics.test.ts`: the upsert-shape test and the smoke-tag test run
  against the statistics handle. The smoke-tag test is the guard for the
  issue's third constraint and must pass unchanged in substance.
- `src/handlers/source-icon.test.ts`: "an unknown source is never fetched"
  and "a known source's icon is served" read the statistics handle.
- `src/handlers/search-worker.test.ts`, `scripts/test-pds-worker.mjs`,
  `scripts/snapshot_refresh/test_search_accounting.mjs`,
  `scripts/snapshot_refresh/route-rehearsal.ts` and its test: each creates a
  `metrics` table in a fixture database and asserts on cache-hit accounting.
  The fixture has to provide the statistics handle.

Add:

- Adapter: `d1Db` returns `results` and `meta.changes` in the shape callers
  use, and `batch` is one transaction.
- Dual write: a failing D1 write does not stop the Turso write, and the
  reverse; neither rejects the response path.
- Backfill arithmetic: the `traffic_sources` and `actors_total` offsets, on a
  fixture where both stores counted across `T0`.
- Rollup: a bucket older than 90 days is added to the daily table once and
  deleted in the same batch, on the statistics handle.
- Materialization parity: the same fixture rows in both stores produce
  byte-identical `stats_data`, `request_data` and `stats_profile` values apart
  from `materialized_at` and `as_of`.
- Regression test for the snapshot limit described under risks.

Smoke suite (`scripts/smoke.py`): `test_stats_page` keeps its seven checks.
Add two that the current suite lacks and that this migration can break:
the page carries a non-zero all-time search total, and a `/stats?range=1h`
fetch made after a tagged search shows a non-empty current bucket, which
proves the tail is read from the live store. `test_cron_selector_plans`
guards corpus selectors only and does not change.

## Risks

- **A limit in the snapshot query is about to cut off new snapshots, with or
  without this work.** `materializeStats` reads
  `SELECT … FROM snapshots ORDER BY hour ASC LIMIT 5000`
  (`src/enrichment.ts:69`), which keeps the oldest 5,000 rows. The public page
  carries 886 thinned points spanning 2026-03-17 to now, which implies between
  4,796 and 4,819 hourly rows (estimate from the thinning rule in
  `src/pages/stats.ts:176-180`). At 24 rows a day the table passes 5,000 in
  the second half of November 2026, after which the newest hours stop reaching
  KV and "actors indexed" on the page and the preview image stops advancing.
  The row count should be confirmed against Turso; the fix is to select the
  newest rows.
- **Dropped writes become visible.** Both stores drop increments
  independently when a write fails. Cutover moves every displayed total by
  the accumulated difference. If Turso has been losing statistics writes
  under contention, D1 will show higher numbers than Turso did.
- **Request counts on 2026-11-06 and 11-07 are 40% of the surrounding days**
  while cache-miss searches are level. Whether that was the ingester calling
  less or statistics writes failing is not known, and it bears on the previous
  point.
- **D1 is single-threaded.** Fine at 4 writes a second; at a hundred times
  current volume the per-answer write would need coalescing.
- **The cron's full-table reads move with the tables.** The daily query scans
  all of `request_buckets` every hour. Inside D1's included reads at current
  size (estimate), but it is the first thing to bound if the table grows.
- **Dual write doubles the per-answer subrequests** for the length of the
  comparison window, and Turso carries its current load throughout.
- **The handle-coverage tile reads 100.0% today** because `with_handles` is
  computed against the unfiltered total and divided by the total minus hidden.
  The migration carries the stored values over as they are; it does not
  correct them.

## Open questions for the owner

1. After issue 31, do the ingester's `/admin/*` calls still pass through the
   Worker? They are 83% of recorded answers. If they stop, recorded volume
   falls by about five sixths and the endpoint view loses its largest series.
2. Who publishes the six corpus-derived numbers once `actors` leaves Turso,
   and how often? The trend chart needs hourly; the exclusions need daily.
3. What difference between the two stores is acceptable at cutover, and over
   what window should the comparison run?
4. Is the 5-minute and hourly history worth carrying, or only the daily
   rollups, snapshots and cumulative totals? Carrying everything is the plan
   above; carrying only the durable rows makes the backfill smaller and the
   28-day profile thin for four weeks.
5. Should `domain` stay on the request tables? Nothing reads it, but it is
   the only record of which app received which status.
6. The repository's own smoke suite sends no `X-Client` header, so its
   searches are counted under `unknown`. Should it send
   `X-Client: smoke-testing`, as the public docs ask third parties to?
7. Is the account's Workers Paid plan confirmed, and is D1 acceptable as a
   second Cloudflare storage product beside KV and R2?

## Not verified

- The DDL and row counts of `traffic_buckets` and `request_buckets`, the row
  count of `snapshots`, and the number of distinct
  `(domain, endpoint, status)` series. All need a read against Turso.
- How quickly Analytics Engine data becomes queryable, and its sampling
  threshold. Neither is documented.
- Whether Durable Object SQLite storage billing is active.
- D1 write latency from the Worker's locations, and D1's behavior under this
  write pattern. Step 2 measures both before anything depends on them.
- The current Turso plan usage attributable to statistics. The committed
  figures are from earlier billing periods.

# Typeahead usage dashboard

Local first pass; no production deployment and no Turso queries. The collector
reads one Cloudflare GraphQL query with hourly, whole-window, and outcome aliases.
The rendered page contains aggregate numbers only, no credentials or client data.

Run from the Typeahead repository:

```sh
python3 ops/usage/collect.py
bun ops/usage/preview.ts
python3 -m http.server 8767 --bind 127.0.0.1 --directory docs
```

Open http://127.0.0.1:8767/drafts/usage/ . The snapshot is manually refreshed by
rerunning collection and rendering. It is not yet a live collector or a complete
cost dashboard. Current `.env` CF_NEW access is sufficient for Workers analytics;
CLOUDFLARE_API_TOKEN can override it. Nothing is persisted from credentials.

## Measurements and interpretation

- Native Workers analytics: invocation count/outcome, fetch subrequests, total CPU,
  CPU quantiles, wall time. Query whole-window quantiles separately: never average
  percentiles. Exclude the incomplete current hour.
- Returned sampleInterval averages are retained in the JSON. Analytics is sampled;
  sum fields are provider estimates, not raw logs or invoice amounts.
- Wall time includes background work. Invocation success is not HTTP success.
  All-route Worker requests cannot serve as a denominator for a search cache ratio.
- The UI currently has saved Cloudflare data; Fly/Turso spend is explicitly dated
  evidence, and Turso screenshots have unverified billing-cycle/account scope.
- No refresh calls from the browser. Future collection should be shared/cached,
  e.g. every five minutes, independent of how many readers open the dashboard.

## Instrumentation priorities

1. Search-only response duration, origin duration, HTTP outcomes, timeouts, cache
   hit/miss, and zero matches. Include the failure-to-empty fallback distinctly.
2. Turso statement/row/byte counts by purpose: accounting, ingest, enrichment,
   overlay pulls, and exports. SQL statements are not billable rows.
3. Export bytes per index build/retry, index bytes and retention, R2 operations,
   overlay rows/apply duration, and time since the last successful build.
4. Account billing dates, allowances, and actual invoice line items. Use daily
   deltas before projecting overages; do not extrapolate a migration day blindly.

Native analytics has no destination field in workersInvocationsAdaptive. Custom
telemetry should avoid search text, actor DIDs, and raw request headers. Consider
Analytics Engine for nonblocking per-search events rather than Turso counter
writes, preserving existing product stats until replacement semantics are tested.

## Read September 5, 2026

- https://developers.cloudflare.com/workers/observability/metrics-and-analytics/
- https://developers.cloudflare.com/analytics/graphql-api/tutorials/querying-workers-metrics/
- https://developers.cloudflare.com/workers/platform/pricing/
- https://developers.cloudflare.com/analytics/analytics-engine/pricing/
- https://developers.cloudflare.com/r2/pricing/
- https://developers.cloudflare.com/kv/platform/pricing/
- https://docs.turso.tech/api-reference/organizations/usage

GraphQL schema introspection verified fields and microsecond units. The documented
Analytics Engine pricing currently says billing is not active; its announced paid
allowance is 10M points and 1M queries/month. Verify account status at rollout.
Workers Standard meters CPU and requests; R2 meters storage/operations (egress
free), KV meters operations/storage. Shared allowances must remain account-wide.

## Access outstanding

Turso CLI session expired; browser opened to login. Need authenticated billing and
usage inspection to attribute 11.3 GB of syncs and $45.40 September invoice.
Application database credentials do not grant billing access. R2/KV dataset access
and Cloudflare account invoice attribution have not yet been validated.

## Revised preview

The costs page now lives in `src/pages/costs.ts` and imports the existing
`src/pages/theme.ts` tokens, theme boot, and theme toggle. Intended route:
`/stats/costs`, linked from `/stats`; this route is not yet deployed or wired.
`ops/usage/preview.ts` renders the page locally. The old independent design was
removed. Current evidence is explicitly dated in this initial budget preview;
collection does not yet update the page automatically.

The earlier ~$80 estimate was withdrawn: it assigned the entire shared Turso
invoice and Workers subscription to Typeahead. The preview now shows only the
~$30/month Fly run rate as an identified subtotal, explicitly not a total.

The user supplied per-database Turso usage: typeahead 523,390,116 reads and
19,902,224 writes; leaf 296,327,134 reads and 480,272 writes; marvin-threads
404,071 reads; foo zero. These do not establish invoice allocation. Need storage,
sync bandwidth, billing dates and overage line items per database. Keep marginal
cost separate from any chosen allocation of the shared base subscription.

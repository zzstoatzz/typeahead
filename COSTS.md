# Costs

What typeahead runs and what it costs. Every dollar figure here is an
estimate, not an invoice. The source for each is named beside it.

## What runs

| resource | shape | estimate a month | source |
| --- | --- | --- | --- |
| `typeahead-shadow-a`, compute | Fly, 4 shared CPUs, 2 GB, always on | $13.25 | hub snapshot |
| `typeahead-shadow-a`, volume | 40 GB | $6.00 | hub snapshot: $0.15 per GB |
| `typeahead-shadow-b`, compute | the same machine | $13.25 | same shape as slot A |
| `typeahead-shadow-b`, volume | 40 GB | $6.00 | $0.15 per GB |
| **the two slots** | | **about $38.50** | |

The hub snapshot is `https://hub.waow.tech/api/costs.json`, read on
2026-11-13. It prices each Fly machine from July 2026 list prices and its
uptime over the last seven days, and each volume at its provisioned size.

```sh
curl -s https://hub.waow.tech/api/costs.json | jq '[.lineItems[] | select(.service|test("^typeahead-"))]'
```

Everything else typeahead uses is on a shared account and is not a typeahead
line item:

- Cloudflare: three Workers, one Durable Object, two D1 databases
  (`typeahead-control`, `typeahead-stats`), one KV namespace and the
  `typeahead-index` R2 bucket. Typeahead's share of that bill has not been
  measured.
- Logfire: the `typeahead` project receives one heartbeat a minute from each
  slot, plus warnings and errors.

## What still bills and should not

These are left over and are listed in [teardown](docs/teardown.md).

| resource | estimate a month | source |
| --- | --- | --- |
| `typeahead-tantivy-a` volume, 20 GB, machine stopped | $3.00 | hub snapshot |
| `typeahead-tantivy-b` volume, 20 GB, machine stopped | $3.00 | hub snapshot |
| old snapshot bundles in the `typeahead-index` R2 bucket | not measured | |
| the Turso database `typeahead`, one database on a shared plan | not measured | |

The hub snapshot still shows compute for `typeahead-ingester` and the two
`typeahead-tantivy` machines because it looks back seven days. All three are
stopped.

## Reading real use before resizing

Fly's metrics are the same data as its Grafana. The token needs the `FlyV1`
prefix, not `Bearer`.

```sh
T=$(fly auth token)
PROM(){ curl -s -H "Authorization: FlyV1 $T" \
  "https://api.fly.io/prometheus/personal/api/v1/query" --data-urlencode "query=$1"; }
PROM 'max by(app)(max_over_time((1-fly_instance_memory_mem_available/fly_instance_memory_mem_total)[3d:5m])*100)'  # memory used %, 3-day peak
PROM 'max by(app)(max_over_time(fly_instance_load_average[3d:5m]))'                                                # load, 3-day peak
PROM 'max by(app)(max_over_time(fly_instance_cpu_throttle[3d:5m]))'                                                # above 0 means throttled
PROM '(fly_instance_filesystem_blocks - fly_instance_filesystem_blocks_free)*fly_instance_filesystem_block_size/1e9' # disk used, GB
```

A slot's four shared CPUs are a quarter of one CPU sustained, with a balance
for bursts. Measure before changing the size, and change it as
[operating](docs/operating.md#resize) describes.

The cost record through October 2026 is in
[history](docs/history/costs-through-20261024.md).

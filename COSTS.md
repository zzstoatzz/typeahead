# Costs

What typeahead runs and what it costs. Every dollar figure here is an
estimate, not an invoice. The source for each is named beside it.

## What runs

| resource | shape | estimate a month | source |
| --- | --- | --- | --- |
| `typeahead-slot-a`, compute | Fly, 4 shared CPUs, 2 GB, always on | $13.25 | hub snapshot |
| `typeahead-slot-a`, volume | 40 GB | $6.00 | hub snapshot: $0.15 per GB |
| `typeahead-slot-b`, compute | the same machine | $13.25 | same shape as slot A |
| `typeahead-slot-b`, volume | 40 GB | $6.00 | $0.15 per GB |
| volume snapshots, both slots | daily, kept 2 days, about 40 GB each | about $13 | $0.08 per GB stored, after Fly's 10 GB free tier |
| **the two slots** | | **about $51** | |

Fly snapshots every volume daily. Retention was 5 days until 2026-11-14, which
at about 40 GB a snapshot would have come to about $32 a month; it is 2 days
now (`fly volumes update <volume id> --snapshot-retention 2`). The snapshot
figure is an estimate until a hub snapshot shows the steady state.

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

## Trailing lines

The hub snapshot looks back over the month, so it keeps showing the destroyed
`typeahead-ingester` and `typeahead-tantivy` apps and the `typeahead-index`
bucket's old 300 GB until those windows pass. The bucket holds 15 KB since
2026-11-14.

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

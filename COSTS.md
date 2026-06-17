# COSTS.md

> **Mandate.** This file tracks the infrastructure costs attributed to **typeahead** and
> what we're doing to bring them down. It is the running record of what this project
> spends, why, and a changelog of changes that moved the number. If the spend here is
> unjustified, that's a signal to clean up — not to ignore it.

## current cost — fetch it live, never hardcode

Costs drift, so this file deliberately does **not** hardcode a dollar figure. Get the
current monthly cost for this repo from the daily snapshot (collected by
`my-prefect-server`, surfaced at https://hub.waow.tech):

```bash
curl -s https://hub.waow.tech/api/costs.json | jq '{
  as_of: .generatedAt,
  this_repo_monthly_usd: (
    [ .lineItems[] | select(.service as $s | ["typeahead-backfill", "typeahead-indexer", "typeahead-ingester", "typeahead-refresh-handles", "typeahead-search"] | index($s)) ]
    | (map(.amount) | add // 0) / 100
  ),
  lines: [ .lineItems[] | select(.service as $s | ["typeahead-backfill", "typeahead-indexer", "typeahead-ingester", "typeahead-refresh-handles", "typeahead-search"] | index($s))
           | {service, provider, usd: (.amount/100), estimated} ]
}'
```

Or open the costs panel at https://hub.waow.tech and group **by project**.

Services attributed to this repo: `typeahead-backfill`, `typeahead-indexer`, `typeahead-ingester`, `typeahead-refresh-handles`, `typeahead-search`. If that list is
wrong, fix the mapping in `my-prefect-server`
(`packages/mps/src/mps/costs/projects.py`) rather than editing numbers here.

## how we might bring this down

Snapshot 2026-06-17 (Fly only ≈ **$43.82/mo**; Turso is separate, ~$30/mo Scaler tier):

| service | $/mo | what it is | biggest cost |
|---|---|---|---|
| `typeahead-search` | 17.76 | App B, always-on search | 4cpu/2GB machine + 50GB vol |
| `typeahead-indexer` | 17.76 | daily snapshot build (~90min/day) | **100GB volume** (idle ~22h/day) |
| `typeahead-ingester` | 6.38 | jetstream → worker | 2cpu/512MB + dead 10GB vol |
| `typeahead-backfill` | 0.96 | leftover from the Jun PLC backfill | stopped machines |
| `typeahead-refresh-handles` | 0.96 | superseded by the worker refresh cron | stopped machines |

### reading actual utilization (do this before resizing — don't guess)

**Fly Prometheus** (same data as the Fly Grafana). The macaroon needs the `FlyV1` prefix,
NOT `Bearer`, and the org slug in the path:
```bash
T=$(fly auth token)
PROM(){ curl -s -H "Authorization: FlyV1 $T" \
  "https://api.fly.io/prometheus/personal/api/v1/query" --data-urlencode "query=$1"; }
# mem used %, peak 3d:   max by(app)(max_over_time((1-fly_instance_memory_mem_available/fly_instance_memory_mem_total)[3d:5m])*100)
# load avg, peak 3d:     max by(app)(max_over_time(fly_instance_load_average[3d:5m]))   # vs cpu count
# cpu throttle (>0=starved): max by(app)(max_over_time(fly_instance_cpu_throttle[3d:5m]))
# disk used GB:          (fly_instance_filesystem_blocks - fly_instance_filesystem_blocks_free)*fly_instance_filesystem_block_size/1e9
```
**Turso**: `turso db inspect typeahead` → storage / rows read / rows written (current period) / sync GB.

### measured utilization — 2026-06-17 (3d window)

| service | mem (avg/peak) | load (avg/peak, vs cpus) | disk used | verdict |
|---|---|---|---|---|
| `typeahead-search` 4cpu/2GB/50GB | 18% / **73%** | 0.51 / **9.55** (4cpu, throttled) | 40 / 50 GB | bursty, NOT over-provisioned — 4cpu earns its keep on promote/resync/cold spikes |
| `typeahead-indexer` 4cpu/2GB/100GB | 17% / 34% | 0.78 / 3.23 (build) | **65.5** / 100 GB | machine fine; build is CPU-heavy, mem-light. Cost = the volume. |
| `typeahead-ingester` 1cpu/256MB/10GB | ~16% / ~31% | **0.01 / 0.22** | **0** / 10 GB | idle; 10GB volume is empty dead weight |

**Turso** (Scaler $30/mo: 100B reads, 100M writes, 24GB): **4.3GB storage (18%), 17B reads (17%),
55M writes (55%)**, 16GB embedded sync. Writes are the binding constraint → can't drop below
Scaler without cutting write volume first. NOT over-provisioned.

**Levers, re-ranked by the data:**
1. **Indexer volume** — build peaks at **65.5GB**, so the 100GB has ~35GB slack but can't go
   below ~70GB. Two options: (a) ephemeral per-build (`fly machine run` create→build→destroy
   daily, needs a trigger) → ~$13/mo; (b) shrink to 80GB (destroy+recreate, can't shrink in
   place) → ~$3/mo. Don't touch mid-build.
2. **Ingester 10GB volume — empty (0 used).** Remove the `[mounts]` + destroy the volume →
   ~$1.5/mo. Clean win.
3. **App B 4cpu→2cpu (~$6/mo) — a TRADEOFF, not free.** Idle on average but load peaks 9.55
   and it's already CPU-throttled at peak; 2cpu would slow the daily promote / resync / cold
   queries. Only if those slower bursts are acceptable.
4. **Turso ~$30/mo** — biggest single line but ~half-utilized on writes; not cuttable without
   reducing write/read volume (optimization work, not a knob). Earlier "over-provisioned" guess
   was WRONG.
- Fly figures are **estimates** from machine inventory — reconcile against the Fly dashboard.

## changelog
- **2026-06-17** — initial cost notice; 5 service(s) attributed here.
- **2026-06-17** — trimmed ~$5/mo: ingester right-sized 2cpu/512MB → 1cpu/256MB (ingest-only,
  no local replica since the MODE refactor); destroyed leftover machines on `typeahead-backfill`
  + `typeahead-refresh-handles` (both superseded). Indexer volume + App B sizing + Turso tier
  are the remaining levers (see above).

# COSTS.md

> **Mandate.** This file tracks the infrastructure costs attributed to **typeahead** and
> what we're doing to bring them down. It is the running record of what this project
> spends, why, and a changelog of changes that moved the number. If the spend here is
> unjustified, that's a signal to clean up — not to ignore it.

## self-reported actual monthly cost (authoritative — read THIS, not the auto-estimate)

**≈ $76/mo** as of 2026-06-17: **Fly ≈ $46 + Turso $30** (+ Cloudflare, see below).

The auto-estimate at https://hub.waow.tech (from `my-prefect-server`) is a **compute-only
lower bound** — its Fly connector counts only `cpus × $1.94 + RAM_GB × $5.00` and **omits
volumes entirely**, and it bills scheduled machines at whatever state they're in *at snapshot
time* (so it counted the daily indexer at full price while a build happened to be running).
This file corrects both. **For typeahead's true cost, trust the table below over the tracker.**

| service | shape | compute/mo | volume/mo | **true/mo** |
|---|---|---|---|---|
| `typeahead-search` | 4cpu/2GB always-on + 50GB | $17.76 | $7.50 | **$25.26** |
| `typeahead-indexer` | 4cpu/2GB scheduled (~90min/day) + 100GB | ~$1.11 | $15.00 | **~$16.11** |
| `typeahead-ingester` | 1cpu/256MB always-on + 10GB (empty) | $3.19 | $1.50 | **$4.69** |
| `typeahead-backfill` | destroyed 2026-06-17 | — | — | **$0** |
| `typeahead-refresh-handles` | destroyed 2026-06-17 | — | — | **$0** |
| **Fly subtotal** | | | | **~$46.06** |
| **Turso** | Scaler tier (corpus reads/writes) | | | **$30.00** |
| **Cloudflare** | worker + KV + R2 snapshots | | | **~$5 (unverified)** |

Rates used (Fly, 2026): shared vCPU **$1.94**/mo, RAM **$5.00**/GB/mo, volume **$0.15**/GB/mo
(volumes bill prorated for the time they exist — key to the indexer plan below). Scheduled
machine compute = full-rate × duty cycle (~6% for a 90-min/day build). Cloudflare is not yet
measured — Workers Paid is $5/mo flat if on that plan; R2 storage for snapshots is trivial
(~$0.20). **TODO: verify the CF line.**

The auto-estimate command (lower bound, compute-only) is still useful as a drift signal:
```bash
curl -s https://hub.waow.tech/api/costs.json | jq '[.lineItems[]
  | select(.service|test("^typeahead-"))] | {compute_only_usd:(map(.amount)|add/100), lines:.}'
```

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

**Levers, re-ranked by the corrected (volume-aware) numbers:**
1. **Indexer 100GB volume → ephemeral per-build (~$14/mo, the only big Fly cut).** A persistent
   100GB volume is $15/mo; the build only needs it ~90min/day. If the build instead creates a
   fresh volume, builds, and destroys it, the volume bills ~$0.30–1.4/mo (prorated). Bonus: with
   an ephemeral volume you can size it generously (150GB+) for ~no cost, so the build never tops
   out as the corpus grows — strictly better than shrinking the persistent one. Needs a daily
   create→build→destroy trigger (see the plan below / `docs/`). **DO NOT shrink the persistent
   volume to 80GB** — saves only ~$3 and risks topping out (build already peaks at 65.5GB and grows).
2. **Ingester 10GB volume — empty (0 used).** Remove the `[mounts]` + destroy the volume → ~$1.5/mo.
3. **App B 4cpu→2cpu = only ~$3.88/mo** (RAM is $10 of the $17.76 and can't drop — mem peaks 73%),
   AND it's a burst tradeoff (load peaks 9.55, already throttled). Small + risky; skip unless desperate.
4. **Turso $30/mo** — ~half-utilized on writes (55%); not cuttable without reducing write volume.
- The hub.waow.tech auto-estimate is compute-only — the table above is the real number.

## changelog
- **2026-06-17** — initial cost notice; 5 service(s) attributed here.
- **2026-06-17** — trimmed ~$5/mo: ingester right-sized 2cpu/512MB → 1cpu/256MB (ingest-only,
  no local replica since the MODE refactor); destroyed leftover machines on `typeahead-backfill`
  + `typeahead-refresh-handles` (both superseded). Indexer volume + App B sizing + Turso tier
  are the remaining levers (see above).
- **2026-06-17** — corrected the cost to self-reported truth (~$76/mo: Fly ~$46 + Turso $30 + CF): the
  auto-estimator is compute-only and omits volumes + mis-times scheduled machines. Indexer's real
  cost is its 100GB volume (~$15), not compute. Next: ephemeral indexer volume (the ~$14 lever).

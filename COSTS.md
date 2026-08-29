# COSTS.md

> **Mandate.** This file tracks the infrastructure costs attributed to **typeahead** and
> what we're doing to bring them down. It is the running record of what this project
> spends, why, and a changelog of changes that moved the number. If the spend here is
> unjustified, that's a signal to clean up — not to ignore it.

## self-reported actual monthly cost (authoritative — read THIS, not the auto-estimate)

**≈ $33/mo** as of 2026-08-29 — typeahead's **dedicated** spend only (the Fly apps + its own
R2 snapshot storage). Shared org infra (Turso, the Cloudflare plan) is **deliberately NOT
counted here** — see below — so the per-project tracker doesn't double-count it across projects.
Target after `docs/replica-removal-plan.md` completes: **≈ $22/mo**.

| service (dedicated) | shape | compute/mo | volume/mo | **true/mo** |
|---|---|---|---|---|
| `typeahead-search` | 4cpu/2GB always-on + **100GB** (was 50GB; filled 2026-08-28) | $17.76 | $15.00 | **$32.76** |
| `typeahead-indexer` (Fly) | app has no machines/volumes since the indexer moved to heavypad | $0 | $0 | **$0** |
| `typeahead-ingester` | 1cpu/256MB always-on, no volume (2026-08-29) | $3.19 | $0 | **$3.19** |
| R2 snapshot storage | ~13.5GB per build × retained builds | | | **~$0.2/build** |
| **TOTAL (attributed to typeahead)** | | | | **≈ $36/mo + R2** |

**Shared org infra — NOT attributed to typeahead (avoid double-counting):**
- **Turso** — a single Scaler plan (~$30/mo) shared across MANY of nate's projects; typeahead is
  one tenant DB on it (4.3GB storage, 55% of the plan's writes — see utilization). Counted once
  at the org level, not here.
- **Cloudflare** — Workers Paid / KV are a shared account plan, not a typeahead line item. Only
  typeahead-marginal CF cost (R2 storage above) is attributed; it's trivial and within plan.

Rates used (Fly, 2026): shared vCPU **$1.94**/mo, RAM **$5.00**/GB/mo, volume **$0.15**/GB/mo
(volumes bill prorated for the time they exist — key to the indexer plan below). Scheduled
machine compute = full-rate × duty cycle (~6% for a 90-min/day build).

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
4. **Turso — shared org plan, not typeahead's line to cut here.** typeahead uses 55% of the
   shared plan's writes though, so trimming its write volume (batch upserts, refresh less) buys
   the ORG headroom / defers a tier bump — track that at the org level, not in this $46.
- The hub.waow.tech auto-estimate is compute-only — the table above is the real number.

## changelog
- **2026-08-29** — measured again (14d Fly Prometheus + logfire) for `docs/replica-removal-plan.md`.
  Ingester right-sized 2cpu/512MB → 1cpu/256MB and its empty 10GB volume destroyed (−$4.69).
  The `typeahead-indexer` Fly app turned out to be already empty (the $16 line above was stale).
  Search volume had to grow 50 → 100GB (+$7.50) after the 08-28 disk-full; the plan takes it
  back to 60GB once the Turso replica is gone, and right-sizes search compute after two weeks
  of post-change metrics.
- **2026-06-17** — initial cost notice; 5 service(s) attributed here.
- **2026-06-17** — trimmed ~$5/mo: ingester right-sized 2cpu/512MB → 1cpu/256MB (ingest-only,
  no local replica since the MODE refactor); destroyed leftover machines on `typeahead-backfill`
  + `typeahead-refresh-handles` (both superseded). Indexer volume + App B sizing + Turso tier
  are the remaining levers (see above).
- **2026-06-17** — corrected the cost to self-reported truth (~$76/mo: Fly ~$46 + Turso $30 + CF): the
  auto-estimator is compute-only and omits volumes + mis-times scheduled machines. Indexer's real
  cost is its 100GB volume (~$15), not compute. Next: ephemeral indexer volume (the ~$14 lever).
- **2026-06-17** — scoped the total to typeahead's DEDICATED spend (~$46/mo, Fly + R2). Turso
  (shared ~$30 Scaler plan) and the Cloudflare plan are SHARED across projects — excluded here so
  the per-project tracker doesn't double-count them.

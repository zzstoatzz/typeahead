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

**Levers, by size:**
1. **Indexer 100GB volume (~$15/mo)** — biggest single waste. The daily build needs ~70GB
   peak but the volume sits idle ~22h/day. Real fix: trigger the build as an EPHEMERAL
   `fly machine run` (create machine+volume, build, destroy) instead of a `--schedule` machine
   with a persistent volume — saves ~$13/mo but needs the worker cron (or a job) to do the
   create→build→destroy daily. Cheap-but-marginal alternative: destroy+recreate the volume at
   ~80GB (can't shrink in place) → ~$3/mo. Don't touch while a build is running.
2. **App B (search) 4cpu/2GB → 2cpu (~$6/mo)** — the 4 CPUs were for cold prefix queries +
   resync; the 60s edge cache absorbs most cold hits. Test: drop to 2cpu, measure cold-query
   p50 on uncached prefixes, revert if it regresses. (50GB volume must stay — ≥4× snapshot.)
3. **Turso ~$30/mo (separate provider, not in the table)** — the single biggest spend overall.
   The Scaler tier was sized for corpus reads, but the serving invariant + edge cache mean the
   request path barely reads Turso. Check actual usage vs tier limits before downgrading.
- Fly figures are **estimates** from machine inventory — reconcile against the Fly dashboard.

## changelog
- **2026-06-17** — initial cost notice; 5 service(s) attributed here.
- **2026-06-17** — trimmed ~$5/mo: ingester right-sized 2cpu/512MB → 1cpu/256MB (ingest-only,
  no local replica since the MODE refactor); destroyed leftover machines on `typeahead-backfill`
  + `typeahead-refresh-handles` (both superseded). Indexer volume + App B sizing + Turso tier
  are the remaining levers (see above).

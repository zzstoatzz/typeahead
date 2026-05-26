# typeahead — cost & scalability

A data-backed read on what the post-cutover architecture costs today, where
the cost concentrates, and how each piece behaves at 10× the corpus. Numbers
captured 2026-05-26, just after cutover (build `b1779755831-4f10`, 5.92M
actors). **Usage figures are measured; dollar figures are estimates from
public pricing — verify before budgeting.**

Companion docs:
- [`turso-usage-analytics.md`](./turso-usage-analytics.md) — how to pull the Turso numbers below
- [`typeahead-index-design.md`](./typeahead-index-design.md) — the serving architecture
- [`retros/2026-05-25-typeahead-rearchitecture-notes.md`](./retros/2026-05-25-typeahead-rearchitecture-notes.md) — the weekend's arc

## The invariant that makes this scale

> No work in `/search?q=…` is proportional to corpus size.

Serving is bounded point-lookups into a precomputed snapshot (`prefix_top_base`
→ top-N DIDs per key → hydrate ≤ `hydrate_cap` from `actor_compact_base`),
merged with a small live overlay. Confirmed in the Turso per-query audit
post-cutover: **zero FTS/prefix request scans remain** — the only
corpus-proportional reads left are batch/cron (sync walk, stats aggregate),
not the request path. This is the property that holds at any corpus size.

## Current cost

### Turso (`turso plan show`, `turso db inspect typeahead [--queries]`)

Plan: **Scaler** (~$30/mo base), period resets monthly.

| Resource | Used | Limit | % | Note |
|---|---|---|---|---|
| storage | 3.6 GB | 24 GB | 15% | fine |
| rows read | 5.26 B | 100 B | 5% | comfortable; should plateau post-cutover (request path no longer reads Turso) |
| rows written | 50.8 M | 100 M | 51% | tightest budget; ingester `last_activity_at` churn + upserts |
| embedded syncs | **29 GB** | **24 GB** | **119% (4.6 GB over)** | the indexer's full-DB export (~3.2 GB) per build; ~6 builds today drove the overage |

Top read consumers (`--queries`, all batch/cron — **none are request-path**):
- sync pagination walk `… WHERE handle != '' AND rowid > ? ORDER BY rowid LIMIT 2000` — search service's incremental/resync
- stats aggregate `SELECT COUNT(*), SUM(CASE…) FROM actors` (~25 M rows) — still a full scan; `actor_deltas` rollup exists but the scan persists (cheap to retire)
- `last_activity_at` batch UPDATEs — ingester write churn

### Fly (`fly machine list`, `fly volumes list`)

| App | Machine | Volume | ~$/mo (est.) |
|---|---|---|---|
| `typeahead-ingester` (A) | shared-cpu-1x / 256 MB | 10 GB | ~$2 |
| `typeahead-search` (B) | shared-cpu-4x / 2 GB | 30 GB | ~$20 (≈$15 compute + ~$5 vol) |
| `typeahead-indexer` | ephemeral (0 idle after cleanup) | 0 (was 40+100 GB) | ~$0 idle + ~$0.50/build compute |

Fly volumes bill ~$0.15/GB/mo. The indexer's 140 GB of idle build-scratch
volumes (~$21/mo) were deleted 2026-05-26 — build scratch should be
**ephemeral** (create → build → destroy). search service was bumped to shared-cpu-4x
for resync throughput; revisit to 2× once steady-state is confirmed (cold-query
latency is RAM/page-cache bound, not CPU bound).

### R2 (`developers.cloudflare.com/r2/pricing`)

~3–4 accumulated artifacts × ~8.6 GB (no pruning yet) at ~$0.015/GB/mo ≈
**~$0.50/mo**, and **R2 has no egress fees** — search service's ~8.6 GB download per
promote is free. Cheapest line item; the accumulation is a cleanliness issue,
not a cost one.

**Total ≈ $50–55/mo** post-cleanup (Turso $30 + search service ~$20 + ingest service ~$2 + R2
~$0.50 + sync overage a few $). The avoidable waste (idle volumes) is already
removed.

## Scalability at 10× (≈ 59 M actors)

| Component | 1× today | 10× behavior | Verdict |
|---|---|---|---|
| **Request path / latency** | ~75 ms warm | unchanged — bounded lookups | ✅ scales |
| **search service RAM (page cache)** | 2 GB / 8.66 GB artifact | working set grows; must scale RAM or cold latency degrades (saw this at 256 MB) | ⚠️ scale RAM with corpus |
| **Served artifact** | 8.66 GB | ~50–85 GB (≈linear in actors) | ⚠️ download + disk grow |
| **Builder emissions** | 244 M | ~2.4 B | ⚠️ build is the bottleneck |
| **Build scratch disk** | ~100 GB | ~hundreds GB–1 TB | ⚠️ ephemeral big volume per build |
| **Turso export / build** | 3.2 GB | ~32 GB | ⚠️ per-build transfer grows |
| **Build time** | ~2.4 h (shared CPU) | hours; `CREATE INDEX` over billions of rows | ⚠️ heavy batch job |

**The 10× release valve: a quality floor on emission.** Most of 59 M actors are
inactive and could never surface in any prefix's top-N, so they don't need a
compact blob or emissions. Gating emission on a quality threshold keeps
`actor_compact_base`, emissions, scratch, and artifact **sublinear** — this is
the single lever that keeps 10× affordable. Not yet implemented; it's the
priority before the corpus grows much.

**Summary:** *serving* scales for free; the *build pipeline* (emissions →
scratch disk → export transfer → CREATE INDEX time) is where 10× cost
concentrates. The `d:` domain-suffix keys were the first taste of this (+69%
emissions, needed a 100 GB scratch volume, though the served artifact only grew
+0.9% thanks to top-N capping).

## Operational realities (does it respect the network?)

| Concern | State |
|---|---|
| **Account deletion** | ✅ overlay tombstone suppresses on next sync (proven) |
| **Moderation hide/show** | ✅ `mod-override` → `actors.hidden` → sync → overlay → suppressed in ~2.5 min (proven hide *and* restore) |
| **Override workflow** | `POST /admin/mod-override {did, action:'hide'\|'show'}` (Bearer `ADMIN_SECRET`); overlay `source=operator` reserved so direct operator overrides survive snapshot promotion (not yet wired) |
| **Freshness** | sync lag oscillates <5 min; self-heals via bulk resync if lag > `SYNC_MAX_LAG_S` (6 h); `/health` exposes `sync_lag_s` |

### Maintenance gaps (none block serving; all needed to be "boring")

1. **Builds are manual** — no scheduled indexer; needs cron + ephemeral-volume create/destroy. Each build costs ~3.2 GB Turso export + ~100 GB scratch + ~2.4 h.
2. **No alerting** — `/health` + `/index/status` exist; nothing pages on stale build / sync lag / failed build / search service down.
3. **No cleanup automation** — R2 artifacts accumulate; indexer machines/volumes did too (cleaned manually).
4. **Token expiry** — the indexer Turso token expired mid-weekend (refreshed to 30 d); search service's could too. Needs longer-lived tokens or rotation.
5. **Operator visibility** — no explain-actor / explain-query / "why does X appear" tools; `/index/status` + `/debug/index` are the seeds.
6. **Stats full scan** — retire the `COUNT(*)` aggregate in favor of the `actor_deltas` rollup.

## Reproduce the analysis

```sh
turso plan show                          # org quota period-to-date
turso db inspect typeahead --verbose     # per-db totals
turso db inspect typeahead --queries     # per-query read attribution (the audit)
fly machine list -a typeahead-search     # machine size
fly volumes list -a typeahead-indexer    # idle build scratch
```

Run `--queries` after any change touching `actors`, or at billing-window
edges: any new query with rows-read in the tens-of-millions is a
corpus-scan that slipped in. See [`turso-usage-analytics.md`](./turso-usage-analytics.md).

## References

- Turso pricing / plans: <https://turso.tech/pricing>, docs: <https://docs.turso.tech>
- Fly.io pricing: <https://fly.io/docs/about/pricing/>
- Cloudflare R2 pricing (no egress fees): <https://developers.cloudflare.com/r2/pricing/>

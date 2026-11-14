# Serving search from several regions

Status: proposal, 2026-11-01. Nothing here is built or deployed.

An uncached search from Tokyo takes about 205 ms; from the US east coast, about
45 to 70 ms. Almost all of the difference is the distance to the one backend in
ewr. Serving from more regions would close it, but the system assumes in many
places that there is exactly one serving backend. This plan removes those
assumptions first, as changes that are correct and useful with one region,
so adding a region later is configuration and cost rather than a redesign.
Prefect server took the same path to horizontal scale: in-memory assumptions
were backed out behind seams such as a pluggable message bus, whose in-memory
implementation remained the default.

## Where the time goes today

Measured from `search_response_timing` in the public Worker over about seven
minutes of live traffic on 2026-11-01, after the route authority moved beside
the transport (commits 6442a76, 9c6878b). Samples are small, a few to 15 per
location.

| ingress | uncached search, typical |
| --- | --- |
| EWR | 42–109 ms |
| DFW, LAX | 90–100 ms |
| LHR, MAN, ZRH, TXL, FRA | 140–160 ms |
| NRT, KIX | 205–215 ms |

Inside that, the transport's own work is the same everywhere: the route step
is about 13 ms and overlaps the origin request, which takes about 24 ms
(engine 2–3 ms). The edge cache lookup on a miss is 3–9 ms. The rest is the
round trip from the user's Cloudflare colo to the transport in IAD. The edge
cache serves about 4% of searches, because typeahead prefixes are mostly
unique, so caching cannot replace nearer backends.

## Target shape

A region is a cell: one Fly backend slot pair, one transport Worker placed
near it, and one route authority created by that transport. Every cell reads
the same Turso source and the same R2 artifacts. The public Worker chooses
a cell for each request from its ingress colo, and on failure tries another
qualified cell before falling back to Bluesky. Each cell is qualified,
promoted and rolled back on its own, under the existing rules, so one
region's failure or refresh never touches another region's serving slot.

With one cell, this is exactly today's system.

## What is already region-independent

- **Backend live updates.** Each Tantivy slot polls Turso read-only
  (`services/tantivy-input/sync.py`) and commits its cursor, watermark and
  policy state with its documents in its own index
  (`services/tantivy/src/engine/source.rs`). There is no shared cursor or
  lease, so N slots can poll independently.
- **Moderation and deletes.** Overrides, domain blocks and deletes are Turso
  writes (`src/handlers/admin.ts`) that every slot pulls on its next poll.
  Nothing fans out to one target.
- **Response identity.** `routeIdentity` and `responseMatchesRoute`
  (`src/search-route.ts`) check a response against the route that selected
  it, which works per route.
- **Route source.** `RouteSource` and `RouteCache` already make the route
  source pluggable with an R2 fallback, and `validatedSearch` is generic over
  the route and the origin fetch.

## Single-region assumptions to remove

Each item is useful at N=1 and keeps today's behavior when there is one cell.

### Routing record

`control/search-routing.json` has one `current`, one `previous` and one
`mutation` lease whose `app` is `a` or `b`
(`src/search-route.ts`, `typeahead_refresh.py`, `typeahead_tantivy_slots.py`
in my-prefect-server). Version 2 would hold a map of cells, each with its own
`current`, `previous` and lease, so two regions can be refreshed or rolled
back without fencing each other. Readers accept v1 and v2 during the change; the controller
writes v2 only after every reader is deployed. The origin allowlist becomes
the set of slot apps per cell.

### Transport and route authority

The transport is one Worker placed at `aws:us-east-1`, and there is one
route authority (`idFromName("route-2")`, `src/search-transport.ts`).
Placement is per Worker script, so each cell needs its own deployment of the
same script, configured with its cell id and placement, and its own authority
named by cell and created by that cell's transport. The object stays where it
was first created; check `coloCode` in the `durableObjectsInvocationsAdaptiveGroups`
dataset after the first request (see [architecture](../architecture.md)).
`/route`, `/health`, `/ready` and `/index/status` should accept a cell so
operators and the controller can address a specific backend.

### Region selection and failover in the public Worker

`src/handlers/search.ts` makes one 3 s attempt to one transport binding,
then falls back to Bluesky for unscoped requests and fails PDS-scoped ones.
Add a pure function from ingress colo to an ordered list of cells, and try
the next cell before Bluesky. Each response must still carry its own
`X-Typeahead-Pds` acknowledgment, and a scoped request must never be
answered by an unscoped fallback. With one cell, the list has one entry.
The time budget across attempts has to stay inside today's 3 s, or fallback
gets slower for everyone during an outage.

### Freshness, health and coverage

`measureFreshness` (`src/handlers/freshness.ts`) probes one
`SEARCH_BACKEND_URL` with a plain fetch and stores one freshness record. The
controller's serving guard and post-promotion canary read the public
`/health/freshness` and `searchActors`, which with geo routing would only
see the cell nearest the controller (Hetzner hel1). Make freshness a
per-cell record plus an aggregate, and have the controller check its own
cell through the transport's cell-addressed paths. Coverage expiry is per
slot (`tantivy-slot.py` stops a slot 60 s before its checkpoint deadline),
so each cell needs its own refresh to succeed in time. The
[October 31 outage](../retros/2026-10-31-search-expiry-outage.md) shows why
this has to be visible per cell before a second cell exists.

### Refresh controller (my-prefect-server)

- Slots are a flat pair, `APPS = ("typeahead-tantivy-a", "typeahead-tantivy-b")`,
  repeated in `typeahead_refresh.py`. Key them by cell.
- The inactive slot is "the other one" of the single `current`.
- `"region": "ewr"` is a literal in the journal, which feeds volume and
  machine creation.
- Fly tokens map one environment variable and block to each app.
- At most one slot may be pending (`typeahead_tantivy_automatic.py`), and
  the deployment runs one at a time. Per-cell pending records and leases
  would allow cells to refresh independently. Whether they should run
  concurrently is a separate decision.
- The release proof binds one guest and volume profile. A cell with a
  different machine size would need its own proof.

The journal already records a region per request, `prepare` accepts an
explicit app, and R2 paths are already per app (`control/tantivy-slots/<app>/`).
These line numbers were read from my-prefect-server `origin/main` (3ef44ac);
the local checkout was behind it.

### Edge cache

`caches.default` is per colo, keyed by `SEARCH_CACHE_GENERATION`, one value
for the whole deployment. While cells serve different builds, one key would
hold answers from different builds. Either include the build of the cell
that answered in the key, or accept up to 60 s of mixed answers per colo
during a rollout.

### Writes through the transport

Request indexing used to push the new row to one backend through the
transport's `POST /admin/refresh`. Tantivy routes rejected it, so it was
removed on 2026-11-01 along with the transport's write path. Every backend
now gets every change by polling Turso, which is what several cells need.

### Operations and benchmarks

`just index-status`, `bench-search`, `bench-ranking`, `smoke` and the
operations runbook assume one live route. They need a cell argument, and
smoke needs to check every serving cell rather than whichever cell answers
the runner.

## Costs and load

Derived from the Fly price table in my-prefect-server
(`packages/mps/src/mps/costs/connectors/fly.py`) and the slot shape (2 shared
CPUs, 1 GiB, 20 GiB volume); not quoted by Fly for this purpose.

| cell | running slot | volume | per month, one slot |
| --- | --- | --- | --- |
| ewr | $6.64 | $3.00 | about $9.64 |
| fra (×1.154) | about $7.66 | $3.00 | about $10.66 |
| nrt (×1.308) | about $8.69 | $3.00 | about $11.69 |

A refresh briefly runs a second slot in that cell. Durable Object and Worker
costs per cell are small at today's volume (one alarm every 700 ms while
searches arrive). The load that grows with cells is Turso reads: each slot
polls on its own, with a per-slot budget of 512 MiB an hour
(`--max-source-bytes`). That budget is not global, and Turso usage is already
close to plan limits for writes, so read volume per cell must be measured
before a second cell is added. Slots pull artifacts from one R2 bucket;
a cold install on a 1 GiB slot took about 38 minutes in ewr, and how much
of that is download is not recorded.

## Risks

- **Divergent answers.** Cells serve different builds and watermarks, so the
  same query can rank differently by location for up to a refresh cycle.
- **More expiry surface.** Every cell can expire on its own. Per-cell
  freshness and alerting must exist first.
- **Source distance.** Turso is in us-east (the indexer is placed in iad for
  it). Polling from fra or nrt adds latency to each page read; whether that
  matters within a 40 s interval is not measured.
- **Long outages.** A slot down longer than the 7-day tombstone retention
  needs a fresh snapshot (`sync.py`).
- **Failover cost.** Trying a second cell adds time before the Bluesky
  fallback. The total budget must not exceed today's.

## Order of work

1. **Cell seams at N=1.** Routing record v2 with one cell, cell-addressed
   transport paths, cell-named route authority, per-cell freshness, the
   colo-to-cells function returning `[ewr]`, and cell arguments for smoke,
   benchmarks and index status. No added cost;
   each change ships under the existing smoke and refresh gates.
2. **Controller seams.** Slots keyed by cell, region from configuration,
   per-cell pending records, leases and qualification, still with one cell.
3. **A shadow cell.** Provision one more cell (fra is the cheaper candidate
   with the larger European share in this sample), refresh and qualify it on
   the normal schedule, but route no traffic. Measure Turso read load,
   install time from R2, freshness lag and the cost.
4. **Route one area.** Send European colos to the new cell with failover to
   ewr, compare latency and correctness against ewr, and keep the rollback
   to one cell a configuration change.
5. **Decide** whether to keep it and whether to add Asia, based on the
   measured gain against the added cost and operating surface.

## Open questions

- Are cells refreshed one at a time or concurrently? Concurrent refreshes
  multiply peak Turso and R2 load.
- Does a far cell fall back to another cell or to Bluesky?
- Should the colo-to-cell table be static configuration or measured, for
  example from `search_response_timing` by ingress colo?
- What is Turso's read headroom on the current plan?

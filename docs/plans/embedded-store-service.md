# Embedded-store service

Status: proposal, 2026-11-10. Nothing here is built or serving. Tracked in issue 31.

The goal is a search backend that owns its data: it follows the relay itself, keeps the actor corpus in an embedded store on its own disk, and feeds its own index. It runs in shadow beside production until it has been shown to agree, then takes traffic through the existing routing record. Turso, the twice-daily export and the build machine go away.

## Evidence so far

- The [2026-11-10 measurement](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/evals/zkv-scale-20261110/README.md) loaded all 13.2 million actors into a zkv directory store: 9.4 GB settled, commits at 2 to 3 ms, cold reads under 200 µs at p99, 60 MB of memory per handle at fence stride 32, and a second process scanning every actor in about 11 s while the writer kept going.
- The same run showed two limits: a bulk load stalls on compaction, and a full compaction needs twice the store's size in free disk.
- The source inventory in issue 31 found about a dozen writers to Turso and several races between them.
- Turso bills the twice-daily export as sync: 153 GB against a 24 GB allowance ten days into the November cycle (issue 39).

## Shape

Each slot is a complete replica. A slot is one Fly machine with one volume running two processes:

- **The store process** (Zig, grown from today's ingester). It is the only writer. It consumes the relay firehose, the labeler stream and the PLC export, resolves identities, fetches profile data, and commits everything to a zkv directory store. It serves reads and a small authenticated write surface over HTTP.
- **The index process** (Rust, today's Tantivy server, serving code unchanged). The store process feeds it changes through the private reconcile endpoints it already has.

Slots A and B both run all the time and do not depend on each other. The routing record points at one; the other is a hot standby and the rollback target. A slot rebuilds its index from its own store while the route points at the other slot.

What this removes: Turso, the Worker's write endpoints, the checkpoint export, the offline build on Hetzner, bundle publication for refreshes, slot installation and admission as a twice-daily event, coverage expiry, and the two HeavyPad flows that write to Turso.

What it keeps: the public Worker (API, cache, rate limits, Bluesky fallback, stats pages), the search transport and its routing record, the relay firehose as the source, our own identity resolution with durable retry, moderation labels and hiding, PDS filtering, and the fail-closed behaviour of PDS-scoped search.

## Store layout

One zkv directory store per slot. Prefixes are one byte, as measured.

| range | key | value |
|---|---|---|
| actor | DID | the actor record |
| handle | lowercased handle, 0x00, DID | empty |
| change log | 8-byte sequence | DID |
| quality walk | inverted score, DID | empty |
| tombstone | deletion time, DID | empty |
| refresh queue | due time, DID | empty |
| identity queue | due time, DID | attempt state |
| labels | DID, label source, value | label fields |
| policy | overrides and blocked domains | entry |
| cursors | relay, labeler, PLC | position |

Rules that follow from having one writer:

- **A cursor is committed with the data it covers.** Today the relay cursor lives in Workers KV and is written after the database transaction. Here both are in one commit, so a crash cannot separate them.
- **Every change appends to the change log in the same commit.** The index bridge reads the log from its own position, which is also stored. There is no polling by timestamp and nothing depends on one-second resolution.
- **Each actor field has one code path that writes it.** The races listed in issue 35 cannot be expressed.
- **Maintenance is stepped between commits with backpressure**, following zlay's collection index and the harness's stall rule.

## Things that write today and where they go

| today | here |
|---|---|
| ingester posts batches to the Worker | store process commits directly |
| identity queue leases through the Worker | a key range in the store, drained in process |
| label endpoints on the Worker | store process applies label frames and updates the actor in one commit |
| hourly cron refreshes moderation and counts | a paced loop in the store process, as tern does |
| HeavyPad flows for identity and enrichment | the same loop; the flows are retired |
| request-indexing and admin overrides | Worker calls the routed slot's write surface; policy is also published so the standby applies it |
| request and traffic statistics | leave Turso separately (issue 34) |

## Things that read today and where they go

| today | here |
|---|---|
| search | unchanged: transport to the routed slot |
| profile lookups and reputation in the Worker | read endpoints on the routed slot |
| Tantivy polling Turso every 40 s | the store process pushes from the change log |
| offline build exporting 7 GB | not needed; a slot rebuilds from its own store |
| corpus counts on the stats page | a scan in the store process, published on a schedule |

## Bootstrap

A new slot starts from the newest published bundle, exactly as the measurement did: extract the actor documents, load them, then follow the relay from a cursor older than the bundle's watermark. Replayed events are idempotent. Later, a slot can start from a copy of its peer's store files.

## Proving it before it serves

1. A shadow slot runs beside production with no route pointing at it.
2. A comparison job checks every actor's handle, hidden state and PDS against Turso, and the canonical ranking and PDS-scope queries against the production index. It runs across at least one production refresh and one restart of the shadow.
3. Kill tests: the store process is killed during commits, compaction and bootstrap, and must reopen with cursors and data consistent. zkv's own docs cover SIGKILL only, so this is our test to write.
4. Cutover is a route change to the shadow slot. Turso stays written until the new path has served for a full day. Rollback is the route change back.
5. Only then does the second slot convert, followed by the removal of the export, the build flow and Turso.

## Open questions

- **Disk.** Twice the store plus two index copies during a rebuild is roughly 40 GB per slot. Today's slots have 20 GB volumes.
- **Upstream load.** Two replicas mean two relay connections and twice the profile fetches against the appview. The pacing has to assume both.
- **Divergence.** Two independent consumers can disagree briefly. Decide what the comparison job tolerates and what triggers a resync from the peer.
- **Policy distribution.** Overrides and blocked domains must reach both slots; the moderation artifact the slots already poll is the likely carrier.
- **Change-log retention.** How long the log is kept, and what a bridge that falls behind it does.
- **Crash safety on Fly volumes.** zkv states its durability contract for ext4; the kill tests need to run on the real volume type.
- **The preflight rule.** `AGENTS.md` forbids preparing a snapshot on the serving machine. A slot rebuilding its own index while not routed is within the rule's intent; the rule text needs updating when this ships.

## Build order

Each step is useful alone and none changes what serves searches until the last.

1. Store module: key layout, change log, cursors, stepped maintenance, kill tests.
2. Store process: today's ingester writing to the store instead of the Worker, on a new Fly app with a volume.
3. Index bridge and bootstrap from a bundle.
4. Comparison job and telemetry.
5. Enrichment, moderation refresh and policy inside the store process.
6. Worker read and write paths through the routed slot; statistics off Turso.
7. Cutover, second slot, retirement.

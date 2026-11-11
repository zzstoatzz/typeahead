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

## Store module

Status: built and tested locally on 2026-11-10 (issue 40). Nothing uses it yet: the ingester still writes through the Worker, and no store exists on any machine.

The module is `services/src/store/`. Its key layout is documented at the top of `keys.zig` and its value encodings at the top of `record.zig`. It differs from the table above in four places:

- **Labels** are keyed by DID, label source, value and a one-byte subject (the account or its profile record). Turso keys a label by subject URI, and the same value can sit on both subjects; without the subject one would overwrite the other.
- **Cursors** also hold the PLC export position, the index bridge's position in the change log, the next change sequence, the change log's floor and the store format.
- **The queues' due times live in the actor record** as well as in the queue keys, so a queue entry is found from its DID without a second index. Tombstones work the same way: a deleted or deactivated actor keeps its record, marked with its status and deletion time, until it is pruned.
- **Policy** is two sub-ranges: blocked domains and per-DID overrides. An actor's record carries a copy of its override.

Decisions the module makes that the rest of the build depends on:

- **What the change log records.** One entry per actor per commit in which the actor's content changed: status, visibility, handle, profile fields, labels, counts, score. Queue scheduling, the hourly activity stamp and check times do not produce an entry, and a write that changes nothing produces none.
- **A consumer below the log's floor is told so.** It must rebuild from a scan of the actors and continue from the floor.
- **Blocking a domain is two steps.** The policy lands at once and governs every later write; actors already stored are brought in line by a sweep of the handle range, a bounded number of keys per commit.
- **Moderation labels have one writer.** The label range holds what the label stream and the labeler's answers say. A profile fetch's labels from the moderation service are treated as such an answer; its other labels are stored on the actor.
- **The stored hidden flag is as of the actor's last write.** A label's expiry takes effect when something next writes or re-evaluates the actor.
- **The handle rule that hides slurs is supplied by the owner.** It is a set of Unicode regular expressions that Zig cannot run; the module takes a predicate.

Tests (`cd services && zig build test`): encoding and ordering for every range, a model-based test of every kind of change against an in-memory model, a child process killed during commits, flushes and compaction steps, a change-log replay, and a reader scanning while another process writes, compacts and collects.

## Index bridge

Status: built and tested locally on 2026-11-10 (issue 42). Nothing runs it yet. Mechanism, crash behaviour and the places the server's contract does not fit are in [the index bridge document](../index-bridge.md).

Decisions later steps depend on:

- **The index's source cursor holds the bridge's position.** It commits with the documents, so it is the authority; the store's bridge cursor is a copy written afterwards, and is safe to trim the change log by.
- **A trimmed log is answered with a full resync**, which also runs at bootstrap and when the index and the store disagree about the log. Until a round has completed the bridge sends no watermark, so the index reports stale rather than fresh.
- **The stored `hidden` flag is the moderation decision.** The bridge sends no policy snapshot, so the index filters on the flag the store computed.
- **The bridge runs on its own thread with a reader handle.** It blocks on the index's commits; position writes go to the writer.
- **`last_activity_at` is not sent to the index**, because the store does not log a change for it.

Open for the later steps: the server's `cutover_source_ready` and the slot supervisor's checks name the Turso poller's cursor and cannot pass for a bridge-fed index, and an index must still be created by `import` before the bridge can fill it.

## Store process

Status: built and tested locally on 2026-11-10 (issue 41). It is not deployed: no Fly app, volume or image runs it, and it has never followed the real relay, labeler or PLC export.

The store process is a second role of the ingester binary, `MODE=store`, in `services/src/store_process/`. `MODE=ingester` is unchanged. Its configuration is the table at the top of `config.zig`; its concurrency model is the comment at the top of `owner.zig`; its HTTP surface is the table at the top of `http.zig`.

- **One owner thread writes.** The relay intake, the identity resolvers, the PLC tail and the label stream build batches of store changes and hand them to a bounded queue. The owner commits what is queued as one store commit, each batch's cursor inside it, and does the housekeeping between commits: stepped maintenance, domain sweeps, tombstone pruning after seven days, a pass that makes lapsed labels stop hiding, change-log trimming below the slowest consumer, and reopening the store after a failed commit.
- **The relay read loop does not wait for commits.** It blocks only when the queue is full, which is the backpressure; a stalled maintainer stops the owner taking from the queue. A pause of that kind is not treated as a dead relay.
- **A start feeds the index before it reads the store for anything else.** The order is `Stage` in `role.zig`: open the writer, read the relay cursor, open the bridge's handle, start the owner and the bridge, start the consumers. Pruning, the expiry pass, trimming, the handle repair, the health counts and the `/store/*` handle wait 30 s, and the first corpus count 300 s. The bridge hands its position to the owner without waiting for room in the queue, so a full queue (the relay catch-up after a start, or one long turn) does not stop its heartbeat. A SIGTERM stops the owner after the commit it is in and the process exits 0.
- **No bloom filter.** A repeat sighting is a point read that changes nothing, at most one write an hour per active account. The ingester's filter is never reset, so its false positives grow for as long as a process lives and hide new accounts that only ever write ordinary records; this role does not carry it over.
- **Reads go through read-only handles.** `/store/*` and the index bridge each have one, refreshed per request or step, so neither can hold a commit back. Each handle keeps table metadata of its own (60 MB a handle at 13.2 million actors in the 2026-11-10 measurement).
- **The index bridge runs in the process** when `ADMIN_SECRET` is set, on its own thread, against the index's loopback admin listener. Its policy is a snapshot of the store's overrides and blocked domains, sent with every push and heartbeat: an index built from a bundle has policy enabled and stops answering searches once its snapshot is 300 seconds old. With a snapshot the index decides visibility at query time from labels, its slur rules and those lists, and ignores the stored `hidden` flag.
- **A new store is loaded by `MODE=store-bootstrap`** from the actor lines `dump-actors` prints and an optional policy file. The cursors and the bridge position land in the last batch's commit. `deploy/shadow-slot/bootstrap.sh` calls it; that script path has not run on a machine.

What differs from production, all covered by named flags in `sql_model.zig` and a test each in `differential_test.zig`:

- **A gone account stays gone until `#account` says otherwise**, and returns with its record. Production deletes the row, so any later event from the account recreates it bare and re-resolves it. This is the one difference a search user could see through the index.
- **The stored `hidden` flag is not what search uses** while the bridge sends a policy snapshot, so its differences stay inside the store and the `/store` reads: no slur rule is applied to it (the role supplies no predicate), a lapsed label stops hiding when the expiry pass reaches it (2,000 label keys every 10 seconds), and a domain block reaches stored actors by a sweep that a crash can leave unfinished. The comparison job reads this flag and must allow for the slur rule.
- **A bundle does not carry everything.** A loaded actor has no description or banner until its profile is seen again, and its activity and update times are the load time.

Tests (`cd services && zig build test`): encoding and ordering for every range, a model-based test of every kind of change against an in-memory model, a child process killed during commits, flushes and compaction steps, a change-log replay, and a reader scanning while another process writes, compacts and collects.

## Index bridge

Status: built and tested locally on 2026-11-10 (issue 42). Nothing runs it yet. Mechanism, crash behaviour and the places the server's contract does not fit are in [the index bridge document](../index-bridge.md).

Decisions later steps depend on:

- **The index's source cursor holds the bridge's position.** It commits with the documents, so it is the authority; the store's bridge cursor is a copy written afterwards, and is safe to trim the change log by.
- **A trimmed log is answered with a full resync**, which also runs at bootstrap and when the index and the store disagree about the log. Until a round has completed the bridge sends no watermark, so the index reports stale rather than fresh.
- **The stored `hidden` flag is the moderation decision.** The bridge sends no policy snapshot, so the index filters on the flag the store computed.
- **The bridge runs on its own thread with a reader handle.** It blocks on the index's commits; position writes go to the writer.
- **`last_activity_at` is not sent to the index**, because the store does not log a change for it.

Open for the later steps: the server's `cutover_source_ready` and the slot supervisor's checks name the Turso poller's cursor and cannot pass for a bridge-fed index, and an index must still be created by `import` before the bridge can fill it.

## Store process

Status: built and tested locally on 2026-11-10 (issue 41). It is not deployed: no Fly app, volume or image runs it, and it has never followed the real relay, labeler or PLC export.

The store process is a second role of the ingester binary, `MODE=store`, in `services/src/store_process/`. `MODE=ingester` is unchanged. Its configuration is the table at the top of `config.zig`; its concurrency model is the comment at the top of `owner.zig`; its HTTP surface is the table at the top of `http.zig`.

- **One owner thread writes.** The relay intake, the identity resolvers, the PLC tail and the label stream build batches of store changes and hand them to a bounded queue. The owner commits what is queued as one store commit, each batch's cursor inside it, and does the housekeeping between commits: stepped maintenance, domain sweeps, tombstone pruning after seven days, a pass that makes lapsed labels stop hiding, change-log trimming below the slowest consumer, and reopening the store after a failed commit.
- **The relay read loop does not wait for commits.** It blocks only when the queue is full, which is the backpressure; a stalled maintainer stops the owner taking from the queue. A pause of that kind is not treated as a dead relay.
- **No bloom filter.** A repeat sighting is a point read that changes nothing, at most one write an hour per active account. The ingester's filter is never reset, so its false positives grow for as long as a process lives and hide new accounts that only ever write ordinary records; this role does not carry it over.
- **Reads go through read-only handles.** `/store/*` and the index bridge each have one, refreshed per request or step, so neither can hold a commit back. Each handle keeps table metadata of its own (60 MB a handle at 13.2 million actors in the 2026-11-10 measurement).
- **The index bridge runs in the process** when `INDEX_ADMIN_URL`, `INDEX_ADMIN_SECRET` and `INDEX_SOURCE_ID` are set, on its own thread. Its policy is null, so the stored `hidden` flag is the whole moderation decision in the index.

What differs from production and is visible, all covered by named flags in `sql_model.zig` and a test each in `differential_test.zig`:

- **Slur handles are not hidden.** The store takes the rule as a predicate and this role supplies none, because the rule is a set of Unicode regular expressions. With the bridge's null policy the index filters on the stored flag alone, so such a handle would be searchable. This must be closed before the role serves: either a matcher in Zig over `src/moderation-regexes.json`, or a policy snapshot from the bridge so the index applies its own rules.
- **A lapsed label stops hiding when the expiry pass reaches it**, not at the instant it lapses: the pass walks 2,000 label keys every 10 seconds. Production's index evaluates expiry at query time.
- **A domain block reaches stored actors by a sweep the owner drives.** A crash between the policy commit and the end of the sweep leaves it unfinished; nothing records that a sweep is owed. No policy change reaches this role yet.
- **A gone account stays gone until `#account` says otherwise**, and returns with its record. Production deletes the row, so any later event from the account recreates it bare and re-resolves it.

Tests (`cd services && zig build test`): the Worker's write-path cases asserted on store state, a differential run against a model of today's SQL, the role killed with SIGKILL at and between commits and compared with an uninterrupted run, intake pausing on a stalled store without tripping the watchdog, and the owner, the bridge and a model index converging through kills.

Not built: checking the bootstrap relay cursor against the relay's first frame, the write surface for overrides and blocked domains, publishing the corpus count to the statistics page, and the Fly app.

## Enrichment

Status: built and tested locally on 2026-11-10 (issue 44). It is not deployed and has never called the real appview, Blacksky or a PDS; the only real request behind it is the one `getProfiles` response captured as a test fixture.

The store role refreshes profiles itself, in `services/src/store_process/enrich.zig`. It replaces three writers that all write Turso: the Worker's hourly `refreshModeration`, and the HeavyPad flows `typeahead-enrich-backfill` and `typeahead-identity-hourly`. Rates and their arithmetic are the table at the top of `config.zig`.

- **The mapping and the score are ports with shared vectors.** `profile.zig` is `extractProfileFields`; `authority_score.zig` is `computeAuthorityScore`. `bun scripts/profile-vectors.ts` runs the TypeScript over a captured `getProfiles` response and synthetic edge cases and writes the expected fields, scores and f32 bits; the Zig test asserts every one, and `bun test` fails when the file is stale.
- **The score carries V8's `log10`.** `Math.log10` is not correctly rounded. Over the integers 1 to 200,000, arm64 V8 differs from x86-64 V8 in the last bit for 116 of them and JavaScriptCore for 3,407. The port equals x86-64 V8 (Node 22.23.4) for every integer from 1 to 2^27. Which engine build the deployed Worker runs was not checked, so rounding to two decimals is what makes today's scores agree across engines, and a score that lands on a rounding boundary could differ by 0.01.
- **One queue, three feeders.** A profile or identity write queues its actor (new accounts arrive this way, once they have a handle). A walk of the quality range queues ranked actors at 20,000 an hour. A walk of the rest of the actor range (live, with a handle, hidden or unscored) queues 10,000 an hour, and only while the queue is keeping up. Both positions are stored in the commit that queues their entries.
- **A lease is the entry's due time.** A fetch settles its entry only if it still carries the lease, so a refresh requested during the fetch stays queued, and a process that dies loses nothing.
- **Pacing assumes two replicas.** Each holds itself to one request every 2 to 2.4 seconds across every host, so the appview sees at most one request a second from both. A failed request pauses the loop, doubling from 30 seconds to 15 minutes.
- **A bundle load queues nothing.** Every loaded actor keeps the bundle's score and is reached by a walk.
- **The corpus count is a reader scan** (`corpus.zig`), hourly by default, reported in `/health` with its duration. Nothing publishes it to the statistics page yet.

Against today, per replica and with the defaults: the cron checks at most 1,000 actors an hour (800 by the quality walk, 200 from the event queue), 24,000 a day. The backfill flow runs once a day for at most 3.5 hours at 50 actors a second, so at most 630,000 a day, and what it finds queued is about 70,000 arrivals a day plus re-touched actors; its real daily counts were not read for this work. This loop can check up to 981,818 a day, of which 480,000 are the quality walk (19,200 today). The size of the ranked pool in a loaded store has not been measured: the role counts it on every corpus scan and `/health` reports the period that results.

What differs from the cron, all four on purpose:

- **A profile fetch never writes a handle.** The cron copies the appview's handle over the stored one (`src/cron.ts`, `refreshModeration`). The appview has been seen to answer `handle.invalid` for handles that resolve and an old handle after a PLC handle change, and the engine drops `handle.invalid` accounts from search. The cron checks at most 1,000 actors an hour; this loop sweeps the corpus, so the same rule reached every account the appview is wrong about. Here a handle from Bluesky or Blacksky that differs from the stored one, an empty stored handle included, queues the actor for the role's own identity resolution, which resolves DID to handle to DID and alone writes handles. A PDS record carries no handle. The flag is `enrichment_verifies_handle`.
- **A profile fetch's moderation labels are the labeler's answer.** They replace an earlier answer and never what the label stream delivered. The cron overwrites `actors.labels` with the response.
- **A PDS record keeps the stored labels.** The cron writes an empty list, which unhides a taken-down account that only its PDS still answers for.
- **A failed takedown probe retries**, and a batch the appview refuses with a 400 is split until the refused DID is alone.

Two bounds keep those disagreements from flooding the identity queue or delaying the relay's and the PLC export's signals, which are served from the same queue in due-time order. An actor whose identity was resolved within a day is left alone, so an appview that stays wrong about a handle costs one resolution per actor per day and cannot loop with the refresh that a resolution requests. And nothing is queued while 100 identity entries are due: enrichment can refresh 40,909 profiles an hour, and however many disagree, it stops adding to the queue once 100 entries are due, so a signal from the relay never waits behind more than that of enrichment's making, plus what the resolvers already hold. An entry enrichment did queue comes due again on each retry the resolver asks for (after 60 seconds and 15 minutes for a handle that does not verify), and those count toward the 100. An actor already in the queue keeps its entry untouched. A disagreement that is passed over is looked at again on its walk's next pass. `/health` counts each outcome under `enrichment.handles`. How fast the eight resolvers drain the queue against real hosts has not been measured.

**The handle repair** undoes what the sweep already wrote. The owner walks the handle range under `handle.invalid` once in the life of a store, 50 holders every five seconds and fewer when 100 identity entries would be due, and queues each live holder for identity resolution. Its position is stored in each step's commit, so a restart resumes it, and `/health` reports it under `handle_repair`. It covers every record whose handle is `handle.invalid`, whoever wrote it: the ones the appview's answer produced are corrected, and the ones this role's own resolution produced are confirmed at the cost of a second look. It does not cover a record that enrichment overwrote with an old but valid-looking handle. Nothing in the store tells those apart from a correct handle: the change log holds a DID per change with no cause and is trimmed, and nearly every enriched record has a profile check later than its identity resolution, because a resolution requests a refresh. Such a record is corrected when the PLC export or the relay next reports its identity, or when the appview catches up and its handle then disagrees with the stored one. A store loaded from a production bundle runs the repair too; production's handles are subject to the cron's rule.

What this does not do that today's writers do:

- **It does not delete.** The cron's dead-actor cleanup cannot fire today either (both selectors require a handle and the cleanup requires none); account removal arrives on the relay.
- **It does not resolve handles for accounts without one.** `typeahead-identity-hourly` fills empty handles from the appview; here that is the identity queue's job, and an actor without a handle is never fetched.
- **It does not serve request-time enrichment.** `/request-indexing` fetches and writes a profile itself, and the reputation page's on-view refresh and `/admin/enrich-profiles` (which `scripts/prioritized-enrich.py` drives) go through `enrichProfileDids`. All three write Turso and need the write surface (step 6). `enrichProfileDids` also overwrites handle, display name, avatar, createdAt and `associated` with empty values where the cron keeps the stored ones; that variant is not ported.
- **It does not hide slur handles**, as the rest of the role does not: the stored flag has no slur rule and the index applies its own.
- **`Date.parse` is narrower.** A `createdAt` that is not an RFC 3339 timestamp earns no age bonus or freshness penalty; JavaScript reads some other forms.

Tests (`cd services && zig build test`): the shared vectors; the loop against a fake appview (batching, the fallback order, backoff, the request rate under a burst, a refused batch, a restart, zero counts, and a reported handle that is `handle.invalid`, stale, or offered to an actor without one); the handle repair in steps across restarts; an enrichment reaching the model index through the bridge; and the differential run, where the cron's row update is restated in `sql_model.zig`.

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
2. A comparison job checks every actor's handle, hidden state and PDS against Turso, and the canonical ranking and PDS-scope queries against the production index. It runs across at least one production refresh and one restart of the shadow. The [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md) is that job's tooling (issue 43). It compares the two Tantivy backends with each other, and, for the corpus, the shadow's store with production's newest published bundle rather than with Turso, since the bundle holds every actor as of its watermark and reading it costs production nothing. The store's read endpoints answer on the shadow machine only, so the corpus walk runs there against views a builder packs from the bundle. As of 2026-11-10 it has only run against local fixtures.
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

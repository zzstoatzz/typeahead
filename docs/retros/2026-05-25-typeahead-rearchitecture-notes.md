# Typeahead Re-Architecture Notes

Working notes for a future blog post. This is intentionally not a polished
post. It is a memory dump of what happened, why it happened, what we learned,
and which parts may be worth turning into a narrative in Nate's own voice.

## Short Version

Over the 2026 Memorial Day weekend, typeahead.waow.tech moved from an emergency
"proxy everything to Bluesky" backstop to serving from our own independent
precomputed prefix index.

The initial production symptom was search becoming unreliable while the
ingester was doing heavy bootstrap/sync work. The deeper problem was not one
bad query or one missing index. The old architecture asked SQLite FTS and
prefix scans to do corpus-proportional work in the request path, while the same
service was also trying to ingest and rebuild indexes. Splitting the service
helped, but it did not fix the fundamental shape: request-time search over a
multi-million-actor corpus was too easy to wedge, too hard to bound, and too
dependent on page cache luck.

The fix became a new architecture:

- Keep a Bluesky proxy as an emergency safety valve while rebuilding.
- Split ingest and search roles.
- Build a precomputed prefix index offline.
- Store the generated snapshot in R2.
- Have search service download, verify, validate, and attach the snapshot.
- Serve `/search` from bounded point lookups into that snapshot plus a live
  overlay for deletes, moderation, renames, and fresh actors.
- Disable local FTS on the search service once the new path owned serving.
- Cut production over only after side-by-side bsky-vs-App-B QA, public smoke,
  and a rollback command staged.

The cutover happened after the final domain-suffix index promoted and
`germnetwork.com` correctly returned the root account plus subdomains. The
public worker then deleted `EMERGENCY_PROXY_BSKY`, leaving typeahead served by
our own prefix index with a one-command bsky rollback still available.

## Timeline

### Saturday, May 23: The Incident And The Backstop

The starting point was not "let's build a search engine." It was production
pain. Typeahead had been stepping on the same rake: bootstrap/sync work
interfered with reads, causing search to be slow or wrong at exactly the times
the system was trying to repair itself.

The emergency move was a Worker kill switch:

- `EMERGENCY_PROXY_BSKY=1` made the public endpoint proxy Bluesky's typeahead.
- This made production responsive and gave us room to work.
- It was explicitly a salve, not the product. The whole point of this service
  is independence from Bluesky.

The first structural move was splitting roles:

- ingest service: ingestion / Jetstream / schema work.
- search service: search role, no Jetstream, no schema migrations, local SQLite copy.

This revealed a second smell: search service could bootstrap off-path, but the old local
SQLite + FTS serving shape still had serious correctness and performance
issues.

### Saturday Night / Sunday: FTS Was Not The Destination

Several attempts improved the old path but did not make it the right path:

- Incremental FTS rebuilds were doing "drop/create/insert 5.9M rows" every
  sync cycle. During the rebuild window, display-name search could be empty or
  partial. This explained bugs like `fig (` not surfacing the expected account.
- Staging-swap FTS rebuilds improved correctness but still meant huge,
  recurring corpus-wide work.
- Per-row FTS maintenance removed the steady-state bulk rebuild, but the request
  path was still vulnerable to wide FTS and prefix scans.
- A shared read connection wedged under concurrent smoke testing. A read pool
  fixed the immediate connection problem, but not the deeper issue: an
  individual query could still occupy a slot for seconds because SQLite was
  doing corpus-scaled work.

This was the key design lesson: pool sizing and timeouts are not enough if the
query plan itself is unbounded. We needed to stop asking the request path to
search the corpus.

### Sunday, May 24: The Precomputed Prefix Index

The design pivot was:

> No work in `/search?q=...` should be proportional to corpus size.

The new model:

- Offline builder emits `prefix_top_base`.
- Each prefix key maps to a score-ordered packed list of DIDs.
- `actor_compact_base` stores the packed presentation data for hydration.
- search service reads a bounded number of keys and hydrates a bounded number of DIDs.
- Request-time work becomes point lookups and bounded merges, not FTS scans.

Core tables:

- `prefix_top_base(key TEXT PRIMARY KEY, dids BLOB)`
- `actor_compact_base(did TEXT PRIMARY KEY, blob BLOB)`
- `index_meta(key TEXT PRIMARY KEY, value TEXT)`
- `actor_overlay(did, blob, hidden, updated_at, source)`
- `prefix_overlay(key, did, score, op, updated_at, source)`

Important design constraint: the base snapshot is not the whole truth. The
overlay is the authority layer for live mutation:

- Deletes/tombstones suppress immediately.
- Hidden/deactivated/moderated actors suppress immediately.
- Renames suppress old prefixes and add new prefixes.
- Manual/ad hoc ingest can bring an actor in before the next rebuild.
- Operator overrides survive snapshot promotion.

This mattered because the base index is stale the moment it is built. Staleness
is acceptable only if it is self-correcting and if urgent suppression/restore
has an immediate path.

### Sunday: Builder Source, R2, And Artifact Promotion

We spent a lot of time on how to get a full source snapshot efficiently.

Rejected or superseded approaches:

- CLI shellout to `turso db export`: fast but not desirable as a load-bearing
  backend mechanism.
- Hrana v3 cursor: streamed correctly, but far too slow for full-corpus
  builder input.
- Hrana v2 pagination: workable fallback, but inefficient compared to export.

The winning source path:

- Use Turso's export HTTP endpoint.
- `GET /info` for generation.
- `GET /export/{generation}` streamed to a local `source.db`.
- Treat the exported main SQLite DB as a consistent checkpoint.
- Read `source_watermark = MAX(updated_at)` from the exported file.

The winning artifact path:

- Use Cloudflare R2, not Fly-managed Tigris.
- R2 matched the rest of Nate's operational stack.
- Use `rclone` for multipart upload/download rather than hand-rolling SigV4
  multipart in Zig.
- Zig still owns correctness: manifest, sha256, size, schema validation,
  sentinel checks, and attach promotion.

Artifact flow:

1. Indexer builds `index.db`.
2. Builder writes `manifest.json` with `byte_size`, `sha256`, build ID,
   versions, counts, and source watermark.
3. Builder uploads `builds/<id>/index.db`, `builds/<id>/manifest.json`, and
   `latest.json` to R2.
4. search service watcher polls `latest.json`.
5. search service downloads into inactive A/B slot.
6. search service verifies size + sha256.
7. search service validates schema, versions, counts, and sentinel queries.
8. search service atomically renames and rebuilds the read pool with the new snapshot
   attached as `idx`.

### Sunday: Builder Cost Surprises

The builder had several "we are 75% through and the shape is wrong" moments.
They were frustrating, but each one identified a real scaling problem.

Findings and fixes:

- `journal_mode=WAL` made artifacts incomplete unless sidecars were shipped.
  Fix: `journal_mode=DELETE` for self-contained `index.db`.
- Long-lived arenas would have grown with corpus size.
  Fix: per-row and per-group scratch arenas.
- Repeated display tokens could duplicate DIDs in a top-N blob.
  Fix: `deriveAllUnique`.
- The staging emissions table bloated the artifact DB even after DROP.
  Fix: attach a separate `staging.db`, delete it after group/pack.
- `temp_store=MEMORY` caused a multi-GB RSS spike during CREATE INDEX.
  Fix: `temp_store=FILE` with temp files on volume.
- Group/pack scanned `(key, score)` index but also needed `did`, causing
  rowid lookups into a huge emissions table.
  Fix: covering index `(key, score DESC, did)`, reducing group/pack from
  hours to minutes.

Full-scale numbers before domain suffix:

- 5.9M actors.
- 8.58 GB artifact.
- Peak RSS around hundreds of MB, not GBs, after fixes.
- Build became viable as an offline batch job.

### Monday, May 25: Serving Path, Overlay, And search service As Primary Candidate

After promotion worked, the real serving path landed:

- `USE_PREFIX_INDEX=1` gates new `/search`.
- Query normalization and key derivation are shared with the builder.
- Base prefix rows and overlay rows are merged.
- Exact handle is hard-promoted.
- Overlay actor blob wins over base actor blob.
- Overlay tombstone/hidden suppresses candidates.
- A live `actors.hidden` safety belt was added for canary.
- Response shape matches the old search endpoint.

Overlay updater landed inside the sync transaction:

- Actor table update and overlay projection happen together.
- Deltas older than or equal to snapshot watermark are skipped.
- Sync-derived overlay rows can be compacted on promotion.
- Operator rows survive promotion.

Sync health became an explicit operational signal:

- `/health` exposes `sync_lag_s`.
- If lag exceeds threshold, search service bulk-resyncs as a self-healing circuit
  breaker.
- Local FTS on search service was disabled with `ENABLE_LOCAL_FTS=0`, because the search
  service no longer needed FTS and FTS maintenance was throttling incremental
  sync.

This was a major simplification: once prefix-index serving owned `/search`, FTS
on the search service became dead weight.

### Monday Night: Domain Suffix Search

A product requirement surfaced late but mattered:

> Searching `germnetwork.com` should show accounts under that domain, including
> subdomains.

The first prefix index only supported:

- `h:` prefixes from the start of the handle.
- `e:` exact handle.
- `n:` display-name token prefixes.

That meant `tessa.germnetwork.com` was findable by `tessa...`, but not by
`germnetwork.com`.

The fix was `d:` domain-suffix keys:

- For `foo.germnetwork.com`, emit dot-suffix keys so queries for
  `germnetwork.com` can find subdomains.
- This was expensive in builder emissions, pushing from about 144M to 244M
  emissions.
- The served artifact barely grew relative to the build scratch cost, but the
  build needed a much larger temporary volume.

This became the last gate. The first check accidentally queried
`germ.network`, which created a false "data gap" scare. The correct query,
`germnetwork.com`, passed:

- `germnetwork.com`
- `tessa.germnetwork.com`
- `anna.germnetwork.com`
- `katherine.germnetwork.com`

This was one of the important product wins over Bluesky: bsky returned only the
root, while our index returned the subdomains.

### Cutover

Before removing the backstop, we ran side-by-side baseline:

- Bsky API:
  `https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead`
- search service direct:
  `https://typeahead-search.fly.dev/search`

Corpus covered:

- exact handles
- handle prefixes
- display-name queries
- multi-token queries
- domain suffix queries
- common short names
- unicode/punctuation-ish cases
- suppression hide/show
- cold/warm latency
- 20 concurrent public burst

Honest comparison:

- search service was faster or competitive on latency.
- Exact handles matched top-1.
- Domain suffix was better than bsky.
- Display and multi-token known cases passed.
- Common short-name ranking was weaker than bsky for some cases such as `jay`.
  This is a ranking-quality issue, not a correctness blocker.

Flip:

1. Set `SEARCH_BACKEND_URL=https://typeahead-search.fly.dev`.
2. Delete `EMERGENCY_PROXY_BSKY`.
3. Public worker now routes to search service prefix index.
4. Rollback staged:

   ```sh
   echo "1" | bunx wrangler secret put EMERGENCY_PROXY_BSKY
   ```

Public smoke passed:

- `zzstoatzz.io`
- `germnetwork.com`
- `fig`
- `nate smith`
- `alex`
- `bad-example.com`

20-concurrent public burst:

- 20/20 HTTP 200.
- p50 around 200ms.
- max around 270ms.

At that point, typeahead.waow.tech was serving from our own prefix index.

## Architectural Lessons

### 1. The Backstop Was The Enabler

The bsky proxy was not capitulation. It was the safety harness that made the
independent system possible. Without it, every experimental fix would have been
done under production panic.

The important distinction:

- bsky as permanent dependency: unacceptable.
- bsky as emergency rollback while replacing the engine: useful.

### 2. Splitting Services Helped, But Did Not Solve The Shape

Separating ingest and search was necessary. It stopped bootstrap from directly
contending with production reads. But search service still needed a serving model that
could not be wedged by one expensive query.

The real invariant became:

> No request should do corpus-proportional work.

### 3. FTS Was Useful For Discovery, Not For The Destination

SQLite FTS helped us understand the problem, but it was not the long-term shape
for this service. FTS maintenance and FTS query behavior were too hard to bound
for typeahead over millions of actors on small infrastructure.

The final search service does not need local FTS for serving.

### 4. Build-Time Cost Is Part Of Serving Architecture

It is not enough for reads to be fast. The index must be rebuildable on a
routine schedule without operator drama.

Every new key namespace needs a preflight question:

- What product behavior does this unlock?
- How many emissions does it add?
- How much scratch disk does it need?
- How much final artifact does it add?
- What happens at 10x?

The `d:` suffix index was the hardest example of this.

### 5. Overlay Is Not A Cache

The overlay is the live authority layer:

- moderation
- deletion
- deactivation
- rename
- restore
- ad hoc ingest

The base snapshot is just a base. The overlay is what lets the service respect
changes before the next build.

### 6. "Some Lag" Is Fine, Permanent Drift Is Not

The goal is not zero staleness. The goal is self-correcting convergence and
operator override.

That means:

- expose sync lag
- define healthy/warn/bad thresholds
- provide self-healing resync
- provide manual suppress/restore/refresh tools

### 7. The Operator Experience Matters

Several painful loops happened because the system was silent:

- group/pack without heartbeat logs
- CREATE INDEX without progress logs
- unclear whether search service sync was advancing
- unclear whether overlay was actually projecting

The future system needs boring answers:

- What build is attached?
- How old is it?
- What is sync lag?
- Is overlay advancing?
- Why does this actor appear?
- Why does this actor not appear?
- What query keys were used?

## Things Worth Including In A Blog Post

Potential title directions:

- "Replacing Bluesky Typeahead Over A Long Weekend"
- "The Search Query Should Not Search The Corpus"
- "How A Typeahead Outage Became A Prefix Index"
- "Building An Independent Atproto Typeahead"

Narrative beats:

1. A production report arrived: typeahead was broken.
2. The immediate fix was to proxy to Bluesky, which felt wrong but bought time.
3. The first instinct was to split the service and fix SQLite/FTS.
4. Each fix revealed a deeper architectural mismatch.
5. The central realization: typeahead needs bounded request-time work.
6. The new architecture emerged: offline prefix snapshots + live overlay.
7. The builder was its own engineering project.
8. Domain suffix search turned into a product win over Bluesky.
9. The cutover was not a heroic flag flip; it was a gated rollout with rollback.
10. The result: own index serving production, bsky only as emergency rollback.

Good concrete examples:

- `fig (` exposed display-name search and FTS/ranking issues.
- `germnetwork.com` exposed domain-suffix semantics.
- `zzstoatzz.io` exact handle and subdomain behavior became a sanity check.
- `jay` exposed the remaining ranking-quality gap.

Good emotional through-line:

- Frustration at stepping on the same rake.
- Refusal to accept "just proxy to Bluesky" as the final answer.
- Repeated "almost there" moments that turned into deeper design lessons.
- The shift from chasing incidents to defining invariants.
- The relief of a boring, measured cutover.

## Remaining Work

Not blockers for the cutover, but important:

- Improve common short-name ranking (`jay`-type cases).
- Make the builder's CREATE INDEX phase observable.
- Revisit `d:` emission cost and maybe optimize the suffix-key strategy.
- Schedule and monitor recurring index builds.
- Ensure R2 tokens do not expire unexpectedly.
- Add richer operator tools:
  - explain actor
  - explain query
  - force refresh/ingest
  - hide/show with audit log
- Keep measuring sync lag and overlay advancement.

## One-Sentence Takeaway

The fix was not "make SQLite faster"; it was changing typeahead from a
request-time search problem into an offline index-build plus bounded lookup
problem, with a live overlay for the parts of the network that cannot wait for
the next snapshot.

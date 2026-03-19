# incident: ingester OOM after widening firehose

## what changed

we widened the jetstream ingester from subscribing to just
`app.bsky.actor.profile` to also include `app.bsky.feed.post`,
`app.bsky.feed.like`, and `app.bsky.graph.follow`. the goal was to
discover more actors — previously we only found people who
created/updated their profile (~92k indexed). now we pick up anyone
who posts, likes, or follows.

commit: `5822553` on main

files changed:
- `ingester/src/main.zig` — added collections, added in-memory dedup set
- `src/index.ts` — bumped `resolveHandles` from 1000→5000/hr with concurrency

## what nate is seeing

on the fly.io dashboard (`fly.io/apps/typeahead-ingester/monitoring`),
the firecracker memory usage shows a **sawtooth pattern** — memory climbs
steadily until it hits the 256MB VM limit, the process OOMs and restarts,
then climbs again. this may be happening every few hours.

nate's exact words: "the firecracker memory usage is in a sawtooth pattern
where it's just ooming every couple hours or every five hours or so."

he also questioned whether the whole approach is sound: "can't we just
design the database intelligently so that it's just always an upsert of
new things? are we doing this ingestion process intelligently or are we
being silly?"

## what i tried (and why it didn't work)

i kept trying to grep fly logs for OOM/restart signals:

```bash
# these all either timed out, returned nothing, or got stuck on streaming
fly logs --app typeahead-ingester 2>&1 | grep -iE "oom|killed|reboot|restart"
fly logs --app typeahead-ingester --no-tail 2>&1 | grep -iE "reboot|connected"
fly logs --app typeahead-ingester 2>&1 | grep -m 20 -iE "reboot|connect|pruning|oom|signal|error"
```

`fly logs` streams indefinitely and the grep patterns weren't matching
anything in the visible window. i should have been looking at the **fly.io
dashboard metrics** directly (memory graph) or used the prometheus/metrics
endpoint, not grepping log lines for kernel OOM messages that may not
appear in application logs.

the one useful command was:
```bash
fly machine status 78465ddb202278 --app typeahead-ingester
```
which showed the VM is 256MB shared-cpu, state `started`, but doesn't
show memory usage history.

## useful commands for debugging this

```bash
# check current ingestion rate and whether it's keeping up
fly logs --app typeahead-ingester 2>&1 | head -20

# check machine status, restarts, event history
fly machine status 78465ddb202278 --app typeahead-ingester

# check VM config (memory, cpu)
fly machine status 78465ddb202278 --app typeahead-ingester --display-config

# check app-level status (machine count, versions)
fly status --app typeahead-ingester

# rebuild ingester locally
cd ingester && zig build

# deploy ingester
cd ingester && fly deploy

# deploy worker
npx wrangler deploy

# check turso DB actor count
turso db shell typeahead 'SELECT COUNT(*) as total FROM actors;'

# check turso write budget
turso plan show

# smoke test search endpoint
curl -s 'https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=nate&limit=3'
```

## my best understanding of the problem

the ingester runs on a 256MB fly.io VM. we added a dedup set
(`std.StringHashMapUnmanaged` + `std.heap.ArenaAllocator`) that grows as
it accumulates unique DIDs from the firehose. the set prunes at 500k
entries by calling `clearRetainingCapacity` and `arena.reset(.retain_capacity)`
— but **"retain capacity" means the memory stays allocated**. the RSS
never actually shrinks. the zig `GeneralPurposeAllocator` also doesn't
eagerly return pages to the OS.

so the pattern is:
1. seen set grows → arena pages allocated, hash map backing array grows
2. at 500k, we "prune" — but retain all the allocated pages
3. new entries reuse the retained pages for a while
4. but hash map resizing, arena fragmentation, and GPA overhead
   accumulate over time
5. eventually RSS hits 256MB → OOM kill → restart → repeat

it's also possible the OOM predates our changes — the old ingester on
256MB may have had a slower version of the same problem with the main
arena. but our changes made it much worse by processing 100x more events.

## the deeper design question

nate is right to question the approach. right now we:
1. receive every post/like/follow on the entire bluesky network
2. extract the DID
3. check an in-memory set (which itself causes OOM)
4. send a bare upsert to the worker
5. the worker does `INSERT ... ON CONFLICT DO UPDATE SET handle = COALESCE(NULLIF('', ''), actors.handle)` — which is a **no-op** for the vast majority of DIDs that are already fully indexed

so we're burning memory, CPU, and turso writes on redundant no-op upserts.

possible fixes:
- **bump VM to 512MB** — buys time but doesn't fix the design
- **drop the seen set, use INSERT OR IGNORE on the worker** — eliminates
  ingester memory growth entirely, worker DB handles dedup naturally
- **bloom filter** — fixed ~1MB memory, no growth, 1% false positive rate
- **worker-side filtering** — have `/admin/ingest` check which DIDs are
  new before writing (read-before-write tradeoff)
- **don't retain capacity on prune** — use `deinit` + re-init instead of
  `clearRetainingCapacity`, so memory actually returns to OS

## current state

the ingester is running and ingesting (~100 actors/batch, ~2 batches/sec).
it went from 92k to 133k actors in ~6 minutes after deploy. it has not
(yet) OOMed since the latest deploy, but the sawtooth pattern nate sees
on the dashboard suggests it will.

DB is at 133k actors. turso writes went from 1.2M to 1.4M (of 25M monthly
budget). search endpoint is working fine.

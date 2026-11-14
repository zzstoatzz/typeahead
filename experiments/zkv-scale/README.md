# zkv-scale

a measurement harness that loads the whole typeahead actor corpus into [zkv](https://tangled.org/zzstoatzz.io/zkv) 0.1.2 on a local directory store and records what that costs in time, disk, memory and latency. it answers the questions in issue 32: could an embedded ordered store hold the corpus that Turso holds today.

status: one corpus-scale run was made on 2026-11-10.

## ownership and storage

owner: the embedded-store evaluation, issue 32.

input is the newest published search bundle in R2, read with the four `INDEX_R2_*` values and nothing else. the bundle's manifest gives its size and SHA-256; the whole archive is verified against both while its Tantivy index files are extracted. every Tantivy document stores the complete actor as JSON, so a small Rust tool prints one actor per line and pipes it straight into the loader. no Turso, Fly or serving-slot access is involved, and nothing is written to R2.

it runs as one temporary, unscheduled Prefect deployment in the `typeahead-ephemeral` pool, on a disposable cpx32 that the worker deletes afterwards. no corpus data comes to HeavyPad or a laptop.

the scratch root is `/var/tmp/typeahead-zkv-scale-<flow run id>`. it holds the Rust toolchain, both build trees, the downloaded bundle, the extracted index, both stores and the command logs. the run needs 80 GiB free at start, stops any phase that takes scratch use past 60 GiB, and has a three-hour limit. every command runs in its own process group under one supervisor, which kills the group on failure, on the deadline and on cancellation. the scratch root is removed in `finally`, and the final summary records free space before and after. the only thing installed outside the scratch root is the distribution's C toolchain package, on a VM that is then deleted. only JSON result lines leave the VM, through the flow run's logs.

## key layout

every prefix is one byte, so the key overhead measured is the real one.

| key | value | written for |
| --- | --- | --- |
| `a` + did | the actor JSON | every actor |
| `h` + lowercased handle + `0x00` + did | empty | actors with a handle |
| `u` + big-endian u64 version + did | empty | every actor |
| `q` + big-endian u32 of the inverted `quality_score` bits + did | empty | live, visible actors with a handle and a positive score |
| `t` + big-endian u64 version + did | empty | deleted actors |
| `r` + big-endian u64 version + did | empty | one actor in eight, chosen by a hash of the did |
| `i` + big-endian u64 due time + did | 16 bytes | one actor in a thousand, chosen by the same hash |

`u` stands in for the `(updated_at, did)` index and `q` sorts higher scores first. `r` and `i` are synthetic: they stand in for the profile refresh queue and the identity queue so their key volume is present, and they do not reproduce either queue's real contents.

the stored `version` is not a per-actor change time. a snapshot import stamps every actor with the snapshot's watermark, and only actors rewritten by live sync after the build carry a later value. so nearly all `u`, `t` and `r` keys in a loaded bundle share one stamp and sort by did within it. key sizes and counts are unaffected; the load result reports the lowest and highest version and how many actors carry the lowest.

duplicate dids in the input would leave a stale `u` key behind. the count phase shows this: `a` entries would be fewer than the records read.

## phases

each phase prints JSON lines labelled with its name, and the run ends with one summary line holding every result.

- **load** reads the actors and commits when staged bytes pass 256 KiB. after each commit one maintenance step runs: flush at 8 MiB of memtable or 256 unflushed segments, one compaction step with a 4 MiB merge budget, table collection when a compaction finishes, and one log collection step. if level 0 passes 12 tables the loader keeps stepping until it is back under the compaction trigger. it reports throughput, commit latency percentiles, the slowest step, stall time, peak RSS, and disk bytes at the peak and the end.
- **count** scans each prefix and reports entries, key and value bytes, and scan throughput. it runs after the load, after settling, and at the end, where it checks that churn and storm left the per-prefix counts unchanged.
- **stat** opens a writer and a reader at fence stride 1 and 32 and reports open time, resident metadata, table shape and RSS. it runs after the load and again after settling.
- **settle** compacts until nothing is planned, collects tables and log segments, and reports time, peak disk and settled size.
- **get** drops the page cache, then times point reads of sampled actors and handle lookups by range scan. the sample is drawn in a separate process first so that sampling does not warm the cache. it runs at fence stride 1 and again at 32.
- **churn** runs the single writer for ten minutes at 30 commits a minute of 60 actors each, with a 3,000-actor burst every two minutes in commits of 400. each update reads the actor, raises its version, rewrites it and moves its version-stamped keys. a second process scans the whole `a` prefix from a reader handle in a loop, in pages of 1,000 with a refresh between pages, counting `error.Missing` and retries.
- **storm** re-sends 1,000,000 existing actors unchanged in commits of 400 as fast as the store accepts them.
- **second load** repeats the load with 64 KiB blocks into a second store for a size and time comparison. it is skipped, with the reason recorded, when the remaining time or disk budget is too small.

every phase after the load can be skipped with the `skip` parameter. `input_jsonl` replaces the download with an actor file already on the machine, and `build_id` pins a bundle instead of taking the newest.

## run

the deployment clones this repository at an explicit commit, so the commit must be pushed first. from the my-prefect-server checkout:

```sh
just prefect --script ~/tangled.org/zzstoatzz.io/typeahead/experiments/zkv-scale/register.py <full commit sha>  # create the deployment
just prefect deployment run 'typeahead-zkv-scale/issue-32'                                                    # start one run
just prefect --script ~/tangled.org/zzstoatzz.io/typeahead/experiments/zkv-scale/register.py --delete         # remove it afterwards
```

## shadow comparison builder
a second temporary deployment reuses the bundle download and `dump-actors` for the [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md). it turns the newest bundle into the sorted views of production as of the bundle's watermark, and publishes them for the [stage that walks the store on the shadow machine](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md#corpus-phase-on-the-shadow-machine). it does not contact the shadow: the store answers on that machine only. status, 2026-11-10: tested locally on 3,000 synthetic actors, publishing to a directory that the shadow stage then read; it has not run in the cloud and has not written to R2.

owner: the shadow comparison, issue 43. input is the newest published bundle in R2, read with the four `INDEX_R2_*` values. output is three objects in the same bucket under `scratch/shadow-compare/<flow run id>/`, written with the same values: `views.jsonl.gz`, `views.jsonl.meta.json` and, last, `manifest.json` with the bundle id, watermark, counts and each object's size and SHA-256. since 2026-11-11 each view line carries a comparison key and the manifest is version 2; the shadow stage refuses objects packed before that, so a run it is to read must come from a deployment registered at a commit that writes the key. those values are the index deployment's `index-r2-*` secret blocks, which publish bundles to this bucket and so can write; a refused write fails the run with the error in its summary. nothing under `releases/` is written, production's serving machines, Turso and Fly are not contacted, and nothing removes the scratch objects afterwards.

the scratch root is `/var/tmp/typeahead-shadow-compare-<flow run id>`. it holds the Rust toolchain and build tree, the downloaded bundle and extracted index, the sorted views and their packed copy. the run needs 80 GiB free at start, stops when scratch use passes 60 GiB, and has a two-hour limit. the bundle, index and toolchain are removed as soon as the views are sorted. every command runs under the same supervisor as the measurement above, and the scratch root is removed in `finally` with free space recorded before and after.

the flow run's logs carry a `published` record and then one summary line; both hold `run_id` and `manifest_sha256`, the two values the shadow stage is given. `input_jsonl` with `watermark` replaces the download with an actor file already on the machine, `build_id` pins a bundle, and `output_dir` publishes to a directory instead of R2.

```sh
just prefect --script ~/tangled.org/zzstoatzz.io/typeahead/experiments/zkv-scale/shadow_compare_register.py <full commit sha>  # create the deployment
just prefect deployment run 'typeahead-shadow-compare/issue-43'                                                            # start one run
just prefect --script ~/tangled.org/zzstoatzz.io/typeahead/experiments/zkv-scale/shadow_compare_register.py --delete        # remove it afterwards
```

## develop

needs Rust, Zig 0.17.0 and uv.

```sh
(cd dump-actors && cargo test --release --locked)        # round-trips a small index with the production schema
(cd zkvscale && zig build test && zig build)             # key encoding, ordering and a store round trip
uv run --no-project --with prefect==3.8.2 python test_cloud.py   # bundle checks, supervision, and every phase on 20,000 synthetic actors
uv run --no-project python synth.py --count 100000 > actors.jsonl  # synthetic actors for a manual run
uv run --no-project --with prefect==3.8.2 --with httpx python test_shadow_compare_cloud.py   # the comparison's builder on 3,000 synthetic actors, then the shadow stage against a fake store
```

the last test builds both tools in a temporary directory and fetches zkv and the Rust crates if they are not cached.

## results

the [2026-11-10 run](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/history/evals/zkv-scale-20261110/README.md) loaded 13,214,296 actors: 9.4 GB settled, commits at 2 to 3 ms, cold reads under 200 µs at p99, and a full reader scan in about 11 s while the writer kept going. bulk loads stalled on compaction and a full compaction briefly doubled the disk used.

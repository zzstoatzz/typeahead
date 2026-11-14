# Offline import tuning

Status: diagnostic experiment, November 5, 2026. No builder settings or running
refresh are changed by this record.

The current importer uses one Tantivy indexing worker, a 128 MiB writer budget
and a commit every 50,000 actors. The cloud builder has four CPUs and 8 GiB RAM.
A ten-second sample near the end of the import measured 9.92 importer CPU
seconds, with about 72% aggregate CPU idle. The Python producer had exited;
the remaining importer was CPU-bound in this sample. This does not separately
profile the earlier streaming phase.

Tantivy supports [multiple indexing workers](https://docs.rs/tantivy/0.27.0/tantivy/index/struct.Index.html#method.writer_with_num_threads)
and splits the total writer budget between them. Merely adding threads with the
same small heap can create smaller segments. Our final `merge(all_segments)` is
one merge operation, so increasing indexing threads alone does not establish
parallel final merging.

## Bounded pilot

Owner: this Typeahead cleanup, using the existing cloud index run's completed
100,000-document sample after its full index import has finished. Reuse the
sample's frozen JSONL and exact reader binary; make no new Turso export and do
not read corpus data through the laptop or HeavyPad.

Scratch: `/var/lib/typeahead-indexer/experiments/import-tuning-20261105` on the
disposable cloud builder, separate from its managed build workspace. Budget
1 GiB, including copied sample input, binary, one trial index and logs. Run one
variant at a time, after publication has completed and the controller is waiting
on the inactive Fly slot. Retain the existing publisher's execution and resource
ownership; never change its process, serving slot, deadline or schedule.

Compare the existing 128 MiB / 50,000 configuration with larger writer memory
and the existing CLI's maximum 100,000-actor batch. Check imported counts,
source/footer identity and matching search responses on exactly the same sample.
Each child has a 90-second limit inside a five-minute supervised experiment.
Delete each trial index after comparison and remove the entire owned scratch on
success, failure or cancellation; record actual free space after teardown.
Only small timing, hashes and resource receipts return locally.

This pilot can identify promising settings, not qualify a full-corpus speedup or
production release. Full-scale optimization still requires matching results on
the same full data, final resource checks and normal admission. Keep serving
memory and live-update defaults unchanged. Parallel indexing or a different merge
strategy requires a separate reader binary and its corresponding validation.

## Sample result

The [cloud receipt](import-pilot-results.json) records five serial variants on
the same input and reader. Both baseline runs took 14.35–14.65 seconds. Raising
writer memory alone to 512 MiB took 14.96 seconds. A 100,000-actor batch with
512 MiB took 12.42 seconds, about 14% below the baseline mean; 1 GiB took
13.53 seconds. All counts, source identity, expiry metadata and eight query
response fingerprints matched. The larger batch avoided the sample's two-segment
final merge and increased peak RSS from about 174 MiB to 291 MiB.

These are single trials on a small partial sample, with baseline checks at both
ends. They do not establish the full-corpus improvement or test parallel
indexing. No production importer settings changed. The experiment finished in
70.47 seconds, peaked at 143,217,302 bytes of scratch and removed its scratch.
Free space after teardown was 129,353,306,112 bytes; the 20 KiB difference from
the starting reading includes concurrent filesystem activity.

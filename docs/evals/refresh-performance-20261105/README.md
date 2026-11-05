# Refresh performance evaluation

Status: implementation and isolated evaluation in progress, 2026-11-05.
Production settings remain unchanged. Earlier 100,000-actor batching results
do not establish full-corpus performance.

Owner: this Typeahead performance task. The first diagnostic uses one temporary,
unscheduled cloud Prefect deployment in `typeahead-ephemeral`, concurrency one,
no production credentials, no serving or rollback slot access. Its temporary
VM owns `/var/tmp/typeahead-import-perf-*`, with an 8 GiB fixture/index budget,
at least 4 GiB free, and a 35-minute total VM limit. A supervised process group
is terminated on timeout or cancellation. Fixture/index copies are removed in
`finally`; only small JSON results are logged. The worker deletes the VM on exit.
No corpus or build dependencies are downloaded to HeavyPad or the laptop.

The diagnostic generates one deterministic million-actor JSONL stream, then
compares the existing settings with four indexing workers, a 2 GiB total writer
budget, larger batches, and one versus eight final segments. Indexing and final
merge timings are recorded separately; the same HTTP query/PDS scope matrix is
compared across the actual readers. A repeated baseline checks order effects.
Synthetic results guide the next experiment; they do not qualify a corpus-scale
rollout. Existing import/footer integrity and admission gates remain mandatory.

The next stages are parallel range downloads, fewer redundant integrity passes,
and performance CPU during inactive-slot installation. Each needs isolated
failure-path tests and measurements before production changes. The serving slot
must never be resized, restarted or used for these experiments.

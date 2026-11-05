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

Diagnostic setup corrections: the first submission had an invalid source pin and
was cancelled. Run `717005e5-2e97-4c8f-b590-445ee1b5d93c` then reached compiler
setup, where Ubuntu's `needrestart` restarted `prefect-flow.service` after libc
development package installation (exit 143). Its archived VM diagnostic confirms
that service restart; it was not an import failure. The diagnostic now uses
`NEEDRESTART_MODE=l` explicitly through sudo. Both temporary VMs were deleted by
the worker; no production deployment or worker was restarted.

The candidate slot downloader writes four 64 MiB ranges directly into one file.
It records completed ranges only after fsync in an atomic sidecar bound to the
immutable object key, size and range size. A retry preserves completed ranges;
old serial partials preserve whole completed ranges too. An interrupted range
can repeat at most 64 MiB. Stat checks reject oversize objects before allocation;
the transfer retains the one-hour deadline, three attempts per range and 1 GiB
free-space reserve. Small metadata transfers retain the serial path. These source
changes are tested locally, not deployed. Real rclone/HTTP failure tests verify
concurrent requests, interrupted-range recovery, binding rejection and byte limits.

The candidate installer hashes archive bytes during streaming extraction,
including padding and trailing bytes. It retains per-member hashes, inventory
validation, binary identity and actual-reader admission before moving the staged
directory into place. This removes the installer's independent full-archive read;
the downloader's cache/transfer hash remains. Corrupt padding/trailing bytes are
rejected and staging is removed. This source change is not deployed either.

Performance CPU during inactive-slot installation remains to be evaluated. The
serving slot must never be resized, restarted or used for these experiments.

The independent CPU diagnostic uses a new `typeahead-perf-20261105` Fly app,
no services or credentials, no volumes, and the existing D13 runtime image.
Two auto-destroy machines compare shared and performance CPU with the same
512 MiB synthetic tar, 4 GiB memory, and a startup-credit drain before measuring
sustained work. Fly requires at least 4 GiB for two performance CPUs; its rejected
1 GiB request created no machine. An additional shared/1 GiB measurement represents
the current serving allocation. Each process has a five-minute alarm and owns one
`/tmp/typeahead-install-cpu-*` scratch directory, bounded at 2 GiB and removed
on exit. This isolates hashing/extraction from networking; it does not qualify
a production installer or authorize a permanent serving allocation change.

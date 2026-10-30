# Full archive PDS join candidate

Status: building and verifying on October 30, 2026; **not admitted or cut over**.
Production routing, source ingestion and authoritative identity handling remain
unchanged. Data processing runs only in the cloud, with no source-database writes.
The user clarified that HeavyPad may be contacted for Prefect control-plane operations.

The existing cloud archive exported 16,249,268 usable mappings from 16,300,003
repo records in 462.48 seconds. The mapping is 1,478,053,888 bytes, SHA-256
`1b1fa39e7b5d8f5224fafb08b23cb3a8e277da680f39ff6899280283ffd65e47`.
These are archive mappings, not the count of Typeahead accounts recovered.
Full checkpoint streaming measures source-known, source-missing, joined and
remaining-missing accounts separately.

The runner calls the existing `fetch_checkpoint`, `build_checkpoint` and
`freeze_bundle` functions directly on the cloud host. This keeps the data build on the cloud host. Prefect control-plane access is
used separately for inactive-slot recovery and qualification. It downloads one source checkpoint and streams actors
into the pinned production Rust binary; it does not write a full JSONL copy.
The mapping is hard-linked into the generation on the same filesystem, then
included in the portable release. A separate content-addressed mapping object
allows later cloud publishers to use the same verified input.

Bounds: metadata export 20 minutes/3 GiB; checkpoint fetch 15 minutes/8 GiB;
import and catch-up 60 minutes; operator process 80 minutes. These are failure
cutoffs, not expected runtimes. Scratch is owned by this candidate under
`/tmp/typeahead-pds-join-full` on the existing archive host. Keep the candidate
until qualification/cutover review; remove redundant checkpoint and test files
after measurements and verification are recorded.

Cloud integration tests: 23 passed, one skipped and one Prefect-context test
excluded; installer/slot tests: 22 passed, three skipped, 136 subchecks passed.
The real Rust reader imported, caught up, froze, installed and reopened the
joined fixture. Missing/corrupted mappings and removed provenance were rejected.
The earlier pilot verifies explicit source migrations and deletions take priority.

Candidate image:
`registry.fly.io/typeahead-tantivy-b@sha256:5720088a146b61f7e559a69036975f958039d1f165cc4790235ac6827bd59056`.
Rust binary is unchanged from production. Only Python import/reconciliation and
artifact handling change. The Dockerfile uses portable COPY plus chmod because
the cloud host's Docker lacks BuildKit.

Prefect confirmed that the older slot preparation run failed. Recovery run
`8b9352bd-2cf8-4b71-b705-42493d489960` uses the existing cancel operation to
release its reservation while retaining the stopped candidate. Serving routing
is not promoted by this operation.

Zig Zen was run. Relevant follow-ups: bind the fallback through every boundary,
fail closed on missing artifacts, include it in disk measurements, and ensure
all child processes and source connections close even if another cleanup fails.
Full-candidate coverage, timing, resource and live-update evidence remain pending.

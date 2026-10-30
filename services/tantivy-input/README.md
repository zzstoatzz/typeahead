# Tantivy source adapter

This directory contains the cloud-only snapshot exporter and read-only Turso
source adapter for the production Tantivy backend. The adapter reconciles source
observations into the serving index. These tools do not change routing or write
to the source database.

`sync.py` defaults to `--source-mode observed`. It discovers changed identities
through indexed timestamp keysets, obtains backend document hashes and a durable
observation revision, then reads those identities from one fresh source SQL
statement before reconciling. Actor timestamps remain source evidence. They do
not order equal-second writes, moderation changes, or unversioned activity
updates. The private reconciliation API orders accepted observations separately;
legacy generic writes remain strict and are disabled after migration.

Current-source reads encode ordered actor columns as bounded JSON tuples in one
SQLite result. Combined profile/policy observations retain one SQL snapshot and
source clock. The decoder validates tuple width, types, identity order and missing
rows before reconciliation. Scores use 17-digit numeric text to preserve source
float-to-f32 rounding. Byte quotas, polling reserves and restart persistence still
charge the actual response body; compact encoding does not bypass those limits.

`--anti-entropy-pages 2` bounds additional identity-sweep pages per polling cycle.
The source-identity sweep discovers new backdated actors; the backend-identity
sweep discovers changed existing actors and source deletions whose tombstones
have expired. Both use indexed identity keysets and at most 500 identities per
page. Cursor progress and the last completed sweep round are durable. A completed
round is a sweep of individual current-state observations, not a transactionally
consistent full-source snapshot. Concurrent changes behind a cursor can require
the next round. Polling freshness and sweep coverage are distinct measures.

The default pacing is conservative, not a qualified full-corpus reconciliation
SLO. A full source-and-backend round visits roughly twice the actor count;
measure a page budget that meets admission deadlines. Polling freshness alone
does not qualify sweep coverage. A source gap beyond seven days still fails closed.
Completed rounds wait `--sweep-cooldown-seconds 3600` before another round;
unfinished rounds resume their durable cursor. `--max-sweep-seconds 5` limits sweep
work between pages in addition to the existing page/cycle limits. One in-flight
page can finish after that soft slice under the existing HTTP deadlines. The first
committed page records the new round's start, excluding cooldown; prior completed
round timestamps stay intact.

`--max-source-bytes` is one shared response-body byte ceiling, including retries. `--poll-reserve-bytes` defaults to one quarter of it (128 MiB of
the default 512 MiB). Sweep consumption cannot use that reserved allocation;
polling can use unused total capacity. Sweep exhaustion returns `sweep_pause:
"budget"` while ordinary discovery and policy observations continue from remaining
capacity. Counters do not reset between cycles or when bulk work pauses. Total
exhaustion still fails polling explicitly until the applicable budget window ends.

Managed slots pass `--budget-file` and `--budget-build-id` to enable shared
`budget.py` accounting. It holds a single-writer file lock, persists actual
poll/sweep bytes with fsync, and reserves the one outstanding capped response
(up to 4 MiB plus one byte) before each HTTP attempt. Completed attempts settle
actual received bytes; a SIGKILL retains that reservation conservatively.
A normal cached restart restores remaining capacity and continues polling,
without another full allowance or an hour-long restart pause. The fixed hourly
window renews only after its durable deadline; sweep cannot consume the 128-MiB
poll reserve. Corrupt identity/accounting fails closed. Offline publisher clients
reuse this factory in cumulative mode with no hourly renewal. Calls without a
budget file retain the original in-process accounting behavior; their owner must
preserve its quota window across process rotation. Consumed body bytes are not a
billing estimate.

Known-ID source pages with no missing backend records use cursor-only revision/CAS
commits, omitting policy and watermark. They do not refresh policy TTL or polling
freshness. Missing actors and backend profile observations still read actor data
and complete policy in one SQL statement. `source_budget` reports poll/sweep usage;
`sweep_pause` identifies budget, cooldown, time slice or discovery priority. None
establishes full coverage or a one-day reconciliation SLO.

`--policy-mode snapshot` is an explicit opt-in for complete operator-policy
snapshots. The selected actor rows, DID overrides, domain blocks and source clock
are read in one SQL statement. At most 10,000 overrides and 10,000 domain rules
are admitted; an extra sentinel row rejects incomplete coverage. The Rust backend
must evaluate the shared canonical policy before accepting candidates, including
show overrides and expired labels. Default `--policy-mode off` preserves the
source-observation rollout until that backend is qualified. See
[the moderation contract](../../docs/evals/tantivy-dev-20261028/moderation-policy.md).

Timestamp mode remains available explicitly for legacy diagnostics and cannot
write after observation-mode migration.

The exporter normally verifies both snapshot files with SHA256. An owned cloud
preparation process can pass `--verified-inputs` and `--verified-inputs-sha256`
to reuse its download verification. The pinned, read-only receipt binds the
manifest digest and each input's digest, inode, device, size and modification/change
times. Any mismatch fails closed. Export still validates the flat format, metadata,
actor count and stream footer, and checks that the files remain unchanged. This
avoids rereading the whole prefix file solely to hash it again.

Run small local checks with:

```sh
python3 services/tantivy-input/test_export.py
python3 services/tantivy-input/test_sync.py
python3 services/tantivy-input/test_current_state.py
python3 services/tantivy-input/test_source_pacing.py
python3 services/tantivy-input/test_rust.py --binary /path/to/typeahead-tantivy
python3 services/tantivy-input/test_rust.py --policy-checks --receipt /tmp/policy-receipt.json
```

Tests use temporary synthetic SQLite sources, loopback HTTP and the compiled
Rust service. See [the observation contract](../../docs/evals/tantivy-dev-20261028/source-observations.md)
for ordering, retry and qualification boundaries. Local source changes are not a
deployment receipt.

Source-only timestamp observations can preserve the stored coarse/last-projection
version while advancing the source revision/cursor and exact request receipt.
Counts, activity and every other Actor field remain real payload changes. The
adapter must not infer latest source time from a stored document timestamp or
claim that replayed unchanged profiles establish missing expiry coverage. See
[the write experiment](../../docs/evals/tantivy-dev-20261028/source-version-only.md).

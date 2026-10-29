# Tantivy source adapter

This directory contains the cloud-only compact-snapshot exporter and read-only
Turso source adapter for the parallel Tantivy backend. No production route or
source database is modified by these tools.

`sync.py` defaults to `--source-mode observed`. It discovers changed identities
through indexed timestamp keysets, obtains backend document hashes and a durable
observation revision, then reads those identities from one fresh source SQL
statement before reconciling. Actor timestamps remain source evidence. They do
not order equal-second writes, moderation changes, or unversioned activity
updates. The private reconciliation API orders accepted observations separately;
legacy generic writes remain strict and are disabled after migration.

`--anti-entropy-pages 2` bounds additional identity-sweep pages per polling cycle.
The source-identity sweep discovers new backdated actors; the backend-identity
sweep discovers changed existing actors and source deletions whose tombstones
have expired. Both use indexed identity keysets and at most 500 identities per
page. Cursor progress and the last completed sweep round are durable. A completed
round is a sweep of individual current-state observations, not a transactionally
consistent full-source snapshot. Concurrent changes behind a cursor can require
the next round. Polling freshness and sweep coverage are distinct measures.

The default pacing is conservative, not a qualified full-corpus reconciliation
SLO. A source gap beyond seven days still fails closed. Native operator-only
moderation rows outside Turso require a separate ownership/migration decision.
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

Run small local checks with:

```sh
python3 services/tantivy-input/test_export.py
python3 services/tantivy-input/test_sync.py
python3 services/tantivy-input/test_current_state.py
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

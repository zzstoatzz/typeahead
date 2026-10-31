# PDS identity coverage

Production uses Tantivy without the old native 10,000-actor PDS cutoff.
Accounts remain absent from scoped search when neither source identity nor the
pinned archive mapping supplies a usable PDS. Empty `actors.pds` in Turso alone
does not imply missing PDS in the serving index.
The existing live identity-event path remains in service.

## Deployed snapshot join

The small [cloud pilot](ops/archive-pds-join-pilot-20261030/README.md) passed
scoped search, profile updates, explicit PDS migration, deletion and restart.
Production uses a read-only SQLite DID→PDS mapping exported from the
existing archive, without copying repository contents or updating Turso.

`pds_fallback` in the imported source manifest binds the mapping checksum,
size and `empty-source-pds-only-v1` policy. Publication includes the mapping;
installation and restart validate it. The live reconciler requires that exact
mapping, using the same actor conversion as snapshot construction and catch-up.
Missing or mismatched required mappings fail closed.

The publisher accepts `INDEX_PDS_FALLBACK_SHA256` to fetch the immutable object
`releases/pds-fallback/<sha256>/pds-fallback.sqlite`. The deployed publisher pins it so subsequent
refreshes preserve joined coverage. Updating the pin takes effect
at the next snapshot build, never by changing a running generation's mapping.
The joined generation was promoted on October 31 and verified through a full
refresh. See [production recovery](ops/production-recovery-20261031/) and the
[qualification evidence](ops/archive-pds-candidate-20261030/README.md).
The verified refreshed index had 12,909,081 actors, with 4,918,517 missing PDS
and two invalid values: about 61.9% usable coverage. These are dated measurements,
not a live coverage guarantee. The mapping is immutable per generation; a new
archive export requires a new pin. Authoritative nonempty source identities take
precedence during live updates.

## Historical repairs

The [retired live-write experiments](archive/pds-live-write-repair.md) and their
receipts explain earlier coverage measurements. No standalone repair schedule
was installed; the obsolete home PLC job remains removed.

### Retained tooling

The older `pds_coverage.py`, candidate sidecar, PLC reconstruction/tail helpers,
and manual repair canary predate this experiment. They are retained historical
tools, not an active pipeline. The canary/ledger retain their original
empty-PDS-only conditional-write behavior and ambiguous-write safeguards.
Neither historical results nor saved archives authorize their execution.

Run their behavioral checks with:

```sh
uv run python -m unittest discover -s scripts -p 'test_pds_*.py'
```

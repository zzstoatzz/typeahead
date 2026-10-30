# Archive PDS join pilot

The October 30 cloud-only pilot passed. It joined archive metadata into a small
Tantivy input stream without updating Turso. Production routing and configuration
were unchanged. This is a partial sample, not a published or admitted snapshot.

## Measured result

The existing Stream cloud host read 1,000 actors from the previously measured
uniform rowid sample using four bounded identity queries (5.72 seconds). This
cohort was selected from actors previously missing PDS; it is not representative
of the entire corpus. Source reads used the existing read-only credential.

Read-only point lookups in the existing RocksDB archive produced 478 candidate
mappings; 519 actors had no archive row and three had excluded hosts. Four actors
already had source PDS values. The join filled 474 of 996 missing values and
preserved every nonempty source PDS. Mapping construction took 2.02 seconds,
including opening the existing archive; the SQLite mapping occupied 49,152 bytes.
No repository contents or full archive/database copies were transferred.

| Measurement | Without join | With join |
| --- | ---: | ---: |
| Tantivy import | 0.253 s | 0.289 s |
| Index bytes | 361,519 | 374,584 |
| Input JSONL bytes | 442,505 | 462,250 |
| Missing PDS in index | 996 | 522 |

The additional read-only mapping is 49,152 bytes. The sampled indexes, source
copy, streams and mapping occupied 2,066,429 bytes at the pre-cleanup measurement.
One run per input at this size establishes mechanics and storage, not a speed
regression or a full-corpus build-time estimate. It ran on the existing archive
host, not a replica of the production Fly allocation.

## Behavioral verification

The pinned production Linux binary was used:
`d1cc0366ed3b28b34daca15d23ee6f06aa1d08844e483c357b80cdb778d5cfa2`.
A disposable SQLite source served real HTTP source observations to the existing
`ObservationWriter`; the Rust admin and search endpoints were loopback-only.

- Ten newly joined accounts appeared in their correct scope and not a wrong one.
- Ten profile updates with empty source PDS retained the joined hosting values.
- An explicit source PDS migration replaced the fallback; the old scope stopped matching.
- Deletion from the sample source removed the actor despite its archive mapping.
- Results survived stopping and restarting the Rust server.
- The source sample was unchanged by conversion/import. Controlled mutations were
  applied only to the disposable sample, never production.
- All 29 cloud adapter tests passed, including malformed/excluded fallback hosts,
  checksum/schema rejection, missing mappings and disabled-mode behavior.

## Implementation and remaining scope

`sync.py` provides a checksum-verified read-only SQLite fallback and an explicit
`pds_fallback(path, sha256)` context around the shared `actor_record()` conversion.
It affects only existing searchable source actors whose PDS is empty. It does not
insert archive-only actors, override source PDS, or suppress deletions. The pilot
uses this conversion for both the partial import stream and live reconciliation.

The production checkpoint publisher and slot supervisor are not configured to
activate or distribute the fallback yet. Before a full candidate build, the
mapping must become a checksum-bound release artifact used consistently by the
checkpoint streamer, catch-up process and serving reconciler. Source provenance
must record the join rather than claim the derived PDS came from Turso. An absent
or mismatched required mapping must fail candidate admission/restart. Refreshing
the mapping belongs with snapshot publication; live authoritative identity events
continue to override it. This pilot does not establish archive-wide freshness.

The owner is this repair pilot. Scratch was `/tmp/typeahead-pds-join-pilot` on the
existing Stream cloud host, with a five-minute process deadline and 512-MiB
per-file ceiling; expected total scratch was below 1 GiB. The runner stopped its
server and removed its sampled database/index files. Final cleanup also removes
the copied executable, code and test files after preserving these receipts.

README review: no production architecture or public API changed, so the root
README does not need a change for this opt-in pilot.

Evidence: `pilot.py` and `result.json`. The older live-database backfill remains
stopped and is not part of this approach.

# Hourly source-poller readiness failures

Status: November 5, 2026, 03:28 UTC. Fix published on main; inactive-slot
qualification is running. Production still serves from A. Times below are UTC.

## Failure

The November 5 03:00 freshness alert reported `search service not ready`, roughly
two minutes of replica lag and a nine-hour snapshot. Coverage was valid and the
latest scheduled refresh had completed. The serving reader had not restarted.
Fly recorded repeated readiness failures:

| failure | recovery |
| --- | --- |
| Nov 4 20:51:25 | 20:51:40 |
| Nov 4 21:52:42 | 21:53:12 |
| Nov 4 22:54:29 | 22:54:43 |
| Nov 4 23:56:06 | 23:56:21 |
| Nov 5 00:57:11 | 00:57:42 |
| Nov 5 01:58:57 | 01:59:27 |
| Nov 5 03:00:58 | 03:01:43 |
| Nov 5 03:02:43 | 03:02:58 |

The source process exits on a 3,600-second SIGALRM. Its supervisor restarts it,
and every startup checks the entire 1.4 GB immutable PDS mapping again. Source
logs contain seven corresponding process-deadline errors. The latest process
started at 02:59:05; its first completed catch-up targeted 03:00:29. Process I/O
counters showed 1,504,616,448 bytes read from disk. These observations establish
the startup gap and repeated file read; they do not separately time hashing,
source HTTP or individual commits.

An interrupted cycle resumes its durable cutoff. Catch-up can span bounded
cycles, with a 40-second sleep after each, leaving the watermark older than the
reader's 120-second readiness limit even after polling resumes. The restart
cadence and resulting lag explain the recurring failures; a candidate observation
past the former hourly deadline remains the production-scale verification gate.

## Change and release scope

`6cc486e` permits `--max-run-seconds 0` only with a durable hourly budget.
`7d20aa1` selects it in the managed slot. Budget rollover already happens inside
the durable source client; process recycling is unnecessary. The 512 MiB hourly
ceiling, 128 MiB polling reserve, HTTP/cycle/sweep bounds, startup checksum and
120-second readiness limit remain unchanged.

The [release Dockerfile](Dockerfile) layers only the new `sync.py` and the single
slot argument change onto the deployed immutable image. The slot script is
`c476fc5` with that argument changed, rather than the complete current slot tree.
This release does not include the separately pending coverage-expiry or native
provenance changes. The [package manifest](package.json) and
[installed file hashes](image-files.json) bind the payload. The reader remains
`ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d`.
Run `uv run --no-project docs/ops/poller-readiness-20261104/prepare-image.py <new-directory>`
from this repository to reproduce the small build context, then build its
Dockerfile for `linux/amd64`.

Candidate image:
`registry.fly.io/typeahead-tantivy-b@sha256:bc66ce9eec3aca5d568135e6d0637f2e2be6e0c305095fc29e4fd50c15d0d34d`.
The managed [preparation run](https://prefect-server.waow.tech/runs/flow-run/4b0c50b9-dd80-4162-9d58-2d7049aed5f4)
installs the existing immutable build `b1793815407-cp-1f545dd4aa` on inactive B,
machine `812056a97999d8`, independent 20 GiB volume `vol_4581g37oj08kxn94`.
The controller owns preparation and cleanup. A remains untouched and retains
valid coverage until November 5 18:03:27. The next scheduled refresh is 06:00 UTC;
finish promotion and persist the new release proof before that run, or reconcile
the manual candidate so it does not fence refresh.

## Verification

Both commits passed the source pacing, durable budget and slot suites separately
through `git rebase -x`. All three Tangled workflows passed at `7d20aa1`.

At two CPUs / 1 GiB, the Linux release image passed the continuous-polling TLS /
SQLite / real-Tantivy regression and the existing adapter suites (37 tests),
[source/policy/crash integration](source-receipt.json),
[15 functional checks](functional.json), and the exact packaged
[slot integration](slot-integration.log) (10 passed, one optional test skipped).
The slot integration exercised actual source polling and restart with the same
persisted hourly budget. One initial integration invocation used a non-executable
tmpfs and failed while executing a copied fixture binary; it passed after the
scratch mount permitted execution. [Test cleanup](test-cleanup.json) verifies
the containers and anonymous fixture volume are gone.

README review: the public interface, architecture and development entry points
are unchanged. The source adapter README documents continuous mode; deployment
state and incident evidence belong here and in operations.

# SQL statement failures during PDS policy replay

Qualified October 20, 2026; not yet deployed.

An isolated replay against a real SQLite-backed HTTPS service returned an HTTP-200
Hrana `type:error` statement response. The original native client treated it as
zero rows and advertised the new scope policy as ready. A scoped query returned
200 with no actor instead of 503. [Retained failure](pds-replay-response-fix/before.json).

The parser now rejects malformed JSON, error/missing statement envelopes,
missing rows and malformed row arrays. Valid empty and populated results remain
valid. The sync loop already handles query errors by retaining incomplete policy
readiness and retrying from the snapshot watermark; no new retry loop is added.

The qualified original service source plus this parser change passed six stages:
HTTP failure, HTTP-200 statement failure, retry from the snapshot watermark,
steady-state resume, rollback and re-promotion. The test used retained full-corpus
old/50k-policy snapshots, a small SQL fixture, persistent overlay state and a
separate threaded loopback evaluation entrypoint. Each injected failure required
proof that its SQL request reached the fixture. No production credentials or
source writes were used. [Result](pds-replay-response-fix/passed.json),
[actual SQL requests](pds-replay-response-fix/queries.json).

The first evaluator attempt lacked concurrent I/O and did not exercise HTTP;
it was discarded as qualification evidence. The retained before/after files
above are from the corrected threaded evaluator. These checks qualify the error
and replay behavior, not an image deployment or full PDS coverage repair.

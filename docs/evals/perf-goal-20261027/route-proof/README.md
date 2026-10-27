# One-second proof reuse — local candidate

The prior conditional-read candidate did not qualify: 19:10–19:25 UTC contained
133 misses averaging 200.58 ms, four hits and 137 reconciled HTTP-200 answers.
It restored transport 40a014e4 and post-rollback smoke passed. Stored component
logs show memory-hint route validation essentially unchanged: baseline 95.996 ms,
conditional candidate 96.182 ms. Copying less JSON did not remove the wait.

The new candidate publishes the existing module cache's original expiresAt as
verified_until in its private shared record. A consumer may borrow it only for
the same origin and build it already knows from R2, for at most the original
one-second lifetime. Unknown/fresh instances still require their own R2 read;
the 60-second hint cannot authorize anything on its own. An independent current
R2 read still starts immediately and only its result can publish a renewed proof.
Cache failure or delay cannot postpone that current read. No TTL or backend
resource change is made. This is local tested behavior, not a deployed win.

Existing Worker/runtime tests pass. The new real workerd/R2/Cache API/HTTP test
proves response release during a held read, original expiry, no lifetime renewal,
and rejection of unknown origins/builds or an oversized lifetime. No corpus or
production requests are used by these tests.

The public handler adds query-free per-response timings: strategy, routed/direct
classification, serving colo and phase milliseconds. This observes existing
requests through the existing log sink, with no new service, monitor or table.
Backend-error empty answers get a distinct outcome. The marker header is internal
to the transport and is not forwarded by the public API. Logging deployment and
transport qualification must be recorded separately.

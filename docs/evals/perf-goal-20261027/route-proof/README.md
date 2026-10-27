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

### Public timing release qualification

Source `5eeae7e` passed all local checks and the retried CI build (the initial
runner did not connect). Public version `a0c42973` initially failed smoke: the
`q=nate` deprecated-parameter comparison saw zero actors while `term=nate`
saw five, so the release automatically restored the previous public version.
That observation is retained, not treated as a pass. A subsequent ordinary
read of each parameter returned five actors on the retained release. The
failure's HTTP status was not preserved by that assertion; smoke now reports
HTTP errors explicitly, including when both requests fail. The same immutable
logging version is being qualified once more with the unchanged full gate.

Initial stored timing events confirm real public requests use the routing
transport, rather than its legacy direct-origin bypass. No proof-reuse transport
version has been deployed during these public logging checks.

The retry passed the full smoke suite at 20:21 UTC. Public timing version
`a0c42973` is live; only its timing seam changed. The bounded proof-reuse
transport trial starts with retained `40a014e4` and candidate `b9cedc8d`, with
10% and 50% smoke/health gates, a fixed ten-minute 50/50 natural-traffic
comparison grouped by ingress location, then (only if that improves) a fixed
15-minute full-traffic counter gate after settlement. The paired phase needs
at least 20 matched-location observations per arm by matched minimum counts;
the full phase needs at least 100 misses, mean <=169 ms, no unexpected statuses,
and complete HTTP/counter reconciliation. Health/freshness and deployment
ownership are checked throughout. The one-hour supervisor restores the retained
transport on failure, interruption or deadline. No Fly resources or traffic
rates are changed. See `canary-run.py` and its terminal `canary/state.json`.

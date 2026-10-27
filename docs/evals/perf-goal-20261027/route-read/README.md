# Bounded route-read comparison

Owner: this Typeahead performance goal. One temporary Worker, read-only operations on the existing route object, no search requests or production mutations. Only method timings and equality flags leave the Worker. Same IAD placement as the transport. Eight-minute code expiry, at most eight sequential diagnostic requests, alternating method order. Full GET includes body consumption. Conditional GET validates the current ETag and should return metadata only for an unchanged object. HEAD is measured for comparison. Delete the temporary Worker after collecting results, including on failure. Scratch/evidence budget below 1 MiB. No production optimization is justified without an observed reduction and correctness qualification.

## Disposition

Retired after zero-percent qualification; no normal traffic used the candidate.
All six exact response pairs matched and actual version IDs were verified by
one bounded native tail. Route timing mean fell 145 to 125.67 ms, but mean
wall time was 306.32 versus 305.45 ms. This does not establish a material
end-to-end gain. The live release already measured 163.47 ms in a settled
public cohort. Its source was retained; the experimental patch is archived
in conditional-candidate.patch. Temporary Worker deletion is confirmed by
CLI and a subsequent API 404.

## Warm-reuse follow-up

The zero-percent sample contained five fresh-isolate/shared-hint candidate
calls and one memory-hint call, versus 13 memory-hint calls among 21 live
version events. The original negative result therefore did not represent
normal ETag reuse. Source 427d320 restores the exact already-tested candidate
for one bounded 10%/50%/100% qualification (warm-canary-run.py). All prior
results remain. Success requires >=100 misses, reconciled counts, no HTTP
errors, healthy freshness, owned deployment and public mean <=169 ms. The
process has a 35-minute deadline and scoped rollback on failure/interruption.
The qualification is not complete while its state is observing. No recurring
monitor, new backend, native resource change or production load test is used.

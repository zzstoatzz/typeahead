# Worker serving traces

This instruments the public Worker and search transport, without changing the
Fly image, placement, route, retrieval, or eval page. Cloudflare's native tracing
provides platform spans; custom spans identify application boundaries. It exports
OTLP/HTTP JSON directly to the existing Logfire `waow/typeahead` project, through
the account destination `logfire-typeahead-traces`. No SDK or telemetry collector
process is added.

## Boundaries

| Span | Boundary |
| --- | --- |
| `worker.handle` | Public handler entry through response return; excludes background statistics completion |
| `worker.rate-limit` | Rate-limiter call |
| `search.handle` | Search handling, including cache or fallback |
| `search.cache.lookup` | Edge cache lookup |
| `search.cache.response` | Cached body identity filtering |
| `search.transport.headers` | Public Worker calls the transport through receipt of headers |
| `search.transport.body` | Consumes the backend response body |
| `search.response.process` | Response validation and identity filtering |
| `search.bluesky-fallback` | Existing fallback, including its bounded body read |
| `transport.handle` | Actual transport entry through response return, before background hint publication finishes |
| `transport.route.validate` | Route acquisition and validation, including cached routes |
| `transport.hint` | Existing memory/shared hint lookup |
| `route.authority.call` | RouteAuthority RPC round trip |
| `route.r2.fallback` / `route.r2.read` | Direct R2 route read |
| `transport.fly.headers` | Each attempted Fly fetch through headers, including a discarded speculative attempt |

Platform spans cover HTTP, service bindings and caller-side Durable Object RPC
operations beneath these custom spans. Nested/parallel durations must
not be added. `search.transport.headers` minus the enclosed transport handler
isolates the boundary residual; it does not by itself distinguish network travel
from platform scheduling. Response body and local processing are separate.
Native invocation spans can include background work; use the explicit handler
spans for response-path accounting.

The public search span and transport handler carry the same
`typeahead.request_id`, based on the public request's CF-Ray (or a generated ID
outside Cloudflare). The internal correlation header is replaced at public ingress
and stripped before Fly. It never enters a cached response. Native trace/span
parentage must be verified in exported records, not inferred from matching IDs.

## Limits and verification

The pinned local workerd is from March 2026 and lacks the custom-span API. A
small adapter runs the same callback without instrumentation there; tests verify
value, promise and error preservation without retrying application work. Live
custom-span readback is required before considering instrumentation operational.
No dependency or compatibility-date upgrade is bundled with this change.

Cloudflare freezes application clocks between I/O events. A zero-duration
synchronous processing span does not prove zero CPU time. Native external trace
context propagation is also limited; this change does not connect Fly internals
or Bluesky internals to the trace. Existing engine timings remain diagnostics,
not synthetic child spans. See [Cloudflare's tracing limitations](https://developers.cloudflare.com/workers/observability/traces/known-limitations/).

Keep client probes spaced and finite. Diagnose one correlated request at a time,
check complete span coverage, then aggregate like-for-like paths. Do not replace
saved eval observations with diagnostics or claim that tracing itself improves
latency.

## Export configuration

Both Workers name the destination in `observability.traces.destinations`; logs
remain in Workers Logs. The destination posts to
`https://logfire-us.pydantic.dev/v1/traces`, with the existing canonical
`logfire.typeahead-write-token` from the encrypted secrets store. Authentication
is held by the destination, not shipped to browsers or added to either Worker.
Rotate by updating that canonical value and the destination's Authorization
header, then verifying delivery in Logfire. No token was rotated for this task.

Both Workers use 5% trace sampling after the initial 100% diagnostic capture.
Logs retain their existing sampling. Separate native trace IDs mean sampling
must not be assumed to retain both Workers for every sampled public request.
For a bounded diagnostic capture, temporarily sample both at 100%, then restore
5%; missing counterpart traces are missing data, not zero-duration work.

## Verified capture — 2026-11-16

Commit `3de9879` was published before deployment. The public version was
`793ac2a7-1cca-430c-a6ec-851f6e54a902`; the transport version was
`9cfc3613-4f5a-4ef0-9e19-8b6589f43778`. Both smoke gates passed.
Three public requests, spaced at least five seconds apart, returned native
Tantivy answers from build `b1794463531-cp-edfe0f3fd1`. No Bluesky comparison or
eval archive write was performed. Exported custom spans confirm that production
supports the tracing API. [Selected trace records](2026-11-16-worker-tracing-spans.json)
preserve the evidence outside telemetry retention.

| Query | Public transport wait | Transport invocation | Transport handler | Before handler within invocation | Outside transport invocation |
| --- | ---: | ---: | ---: | ---: | ---: |
| `zzstoatzz.io` | 80 ms | 45 ms | 31 ms | 14 ms | 35 ms |
| `na` | 72 ms | 35 ms | 21 ms | 14 ms | 37 ms |
| `aoc` | 77 ms | 39 ms | 26 ms | 13 ms | 38 ms |

The unexplained 49–51 ms is therefore outside the transport application handler:
13–14 ms is inside its native invocation before handler entry, and 35–38 ms is
outside that invocation but inside the caller's fetch. These are duration
differences, not subtraction of unsynchronized timestamps from separate hosts.
The platform spans do not distinguish network travel, dispatch, placement or
scheduling within these intervals. Do not label them as pure network latency or
cold starts. Placement remains `aws:us-east-1`; no placement experiment was run.
The exported `cloudflare.colo` is ORD even on transport spans, while outgoing
fetch Ray IDs end in IAD; those fields alone do not establish execution location.

Route validation took 8–11 ms and Fly headers 17–21 ms, with overlap on hint hits.
Engine response headers reported 3.4–6.8 ms. Public body consumption and response
processing have no separately resolved wall time in these traces. Background
statistics extend the public native invocation after `worker.handle` finishes;
they are excluded from the response-path totals above.

The Workers export **separate native trace IDs with no span links** in this
capture. Correlation is verified by the identical `typeahead.request_id` on
`search.handle` and `transport.handle`, not native parentage. The RPC caller's
`jsRpcCall` spans resolve 8–11 ms, but the first `route.authority.call` wrapper
ended immediately around the RPC thenable. Its callback now returns a native
async promise so its lifetime follows RPC completion. Server-side RouteAuthority
execution is not attributed by this capture.

The instrumentation is useful in Cloudflare's viewer as well as Logfire. Logfire
is an existing OTLP destination, not a requirement for diagnosing this boundary.
No collector, SDK, Fly deployment or eval-page change is involved. The initial
change passed 238 Worker and 77 Python tests, lint, type checking and dry-run
builds; the RPC callback adjustment also passed the 11 route/tracing tests.

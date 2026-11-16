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

Platform spans cover HTTP, service bindings, Durable Object RPC handlers and
storage operations beneath these custom spans. Nested/parallel durations must
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

The initial diagnostic rollout retains the pre-existing 100% trace sampling;
reduce sampling after verifying linked spans and capturing bounded evidence.

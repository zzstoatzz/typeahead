# Cold edge latency, October 20, 2026

Investigation in progress. Timing and the loading-state correction are live in
Worker `ee8042ff-439d-4657-87d1-9fea7af8d4ad`. The full authenticated live smoke
suite passed from HeavyPad; local checks passed 92 tests / 589 assertions,
TypeScript and Oxlint. Desktop preview and live 390px mobile checks verified
populated results, stale-result clearing and the closed filter. Engine latency
is not end-to-end latency.

Logfire's recent 43 sampled compact `search` spans had a 2.66 ms median,
7.08 ms p95 and 7.80 ms maximum. These spans start inside the search engine;
they do not include transport or waiting before the handler.

Worker response timing isolates cache lookup, origin response headers, full
origin body and search handling. A cache hit replaces the stored timing with
its own current cache lookup timing. `origin_headers` includes the Fly request
ID so its ingress region can be correlated with the public `CF-Ray` region.
No actor response bytes, cache keys, TTLs, fallback policy or ranking changed.

Twenty paced live searches from HeavyPad (10 global, 10 scoped, limit 8) all
returned HTTP 200 and confirmed cache misses. Median wall time was 867 ms,
maximum 2,166 ms. Requests entering Cloudflare ATL reached Fly through GRU
(São Paulo) and SYD (Sydney), while the serving machine is in EWR (Newark).
One reproduced request spent 1,828 ms waiting for origin headers versus 29 ms
on cache lookup. This proves a slow origin hop and a geographic detour; it
does not establish how much each network or pre-handler queue contributed.

An IPv4-only origin alias is being prepared as a routing experiment. It is
not yet serving traffic. The existing `.fly.dev` origin remains unchanged.
DNS access is currently blocked: available API tokens lack DNS permission and
the dashboard fails to load authorization policy. A Fly certificate request for
`typeahead-origin.waow.tech` is pending. Required DNS-only records are A
`typeahead-origin` → `66.241.125.33`, TXT
`_fly-ownership.typeahead-origin` → `app-1pk5pr0`, with no AAAA. Verify DNS, TLS,
origin identity, paired response equality and live timings before switching.
Do not claim a speedup until paired live measurements verify it.

The homepage no longer inserts a transient “Searching…” results dropdown.
Pending searches clear stale query/scope results and set `aria-busy`; only
results, empty answers or errors open the dropdown. The filter remains closed
by default.

Raw evidence and the reproducible paced probe script:
`~/.local/share/typeahead-experiments/edge-latency-20261020/`.
The initial PDS-capable Worker rollback is
`36477826-a11c-426b-807b-88dd9f331aba`; refresh deployment state before rollback
and preserve any newer unrelated source. Backend images, snapshots and recovery
pairs are unchanged.

# October 31 production recovery

Production was restored to verified Tantivy B at 16:08:50 UTC. The public Worker
now automatically uses Bluesky when unscoped backend searches fail, and returns
to the primary on subsequent successful requests. PDS scopes fail explicitly
when the primary cannot serve them.

- [Incident and future experiment checklist](../../retros/2026-10-31-search-expiry-outage.md)
- [Real automatic fallback before promotion](automatic-failover-live.json)
- [Public aliases and PDS checks after promotion](public-restored.json)
- [Public smoke suite](public-smoke.log); admin-authenticated and database-plan checks were not run in this public-only pass
- [Conditional promotion routing and operation receipt](promoted-route.json)
- [Worker runtime failure and recovery tests](worker-runtime-tests.log)
- [Publisher configuration](publisher-config.json) and [concurrency readback](publisher-concurrency.json)
- [Full refresh request](refresh-request.json), still awaiting completion
- [Cloud controller resource check](controller-resources.json)

Worker version: `6e57ccd1-782e-4674-8f3e-e6c884c89fac`.
The manual emergency flag was removed only after automatic fallback was deployed
and tested against the then-unavailable primary. Successful B responses have no
failover header. Existing normal query caching remains; fallback responses are
not cached.

The running full refresh is verification of the normal deployment, not a separate
experimental publisher. Its completion and resulting admission remain required
before declaring the refresh repair finished. Source processing stays in the cloud.

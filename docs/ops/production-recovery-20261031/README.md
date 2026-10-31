# October 31 production recovery

Production was restored to verified Tantivy B at 16:08:50 UTC. The normal refresh
then built, published, admitted, restarted and promoted a fresh A generation;
its resumed flow completed at 17:59:36 UTC. B remains available for rollback.
Automatic Bluesky fallback is deployed independently in the public Worker.

The full checkpoint build took 46 minutes and produced a 6.32 GB bundle. Cold
installation and first readiness took about 38 minutes on 2 shared CPUs / 1 GiB
RAM / 20 GiB disk. The publisher now allows one hour per readiness phase within
its three-hour outer bound. These are measured build/installation times, not
search latency. The successful resume used controller `f471239`; current pin
`35c26b4` adds only the measured readiness-timeout margin to that runtime code.

The first run hit a rejected Fly start (HTTP 412). The repaired controller resumed
the same artifact and machine; it did not repeat the export, build or publication.
All 45 controller/automatic-refresh tests passed, including real HTTP rejection,
resume, bounded retry, uncertain outcomes, route changes and configuration drift.

Public smoke and ranking acceptance passed after the refresh. Both API aliases
return the new build, and scoped tests include a joined account plus an unrelated
PDS that must return no matches. The live reader reported 12,909,081
actors, 4,918,517 missing PDS values and two invalid
values: 61.90% valid recorded PDS coverage. The remaining source gap is
unchanged by the reliability repair.

Coverage expires at `2026-11-01T16:12:53+00:00`. The next scheduled run is November 1 at
06:00 UTC. The October 31 18:00 occurrence was canceled before flow execution
because the verified refresh completed seconds before it; the recurring
06:00/18:00 UTC schedule remains enabled. Deployment concurrency is one, with
`CANCEL_NEW` and no occupied slots at final readback.

All cloud controller VMs from this recovery and the redundant occurrence were
deleted. The expired A machine and volume were retired. Artifact retention
removed 9 obsolete objects totaling 33,140,849,322 bytes. A, retained B, and the
native rollback each retain their existing 1 GiB / 20 GiB allocation; the ingester
is unchanged. Retained rollback resources are intentional, not build scratch.

## Evidence

- [Incident and future experiment checklist](../../retros/2026-10-31-search-expiry-outage.md)
- [Real automatic fallback before B promotion](automatic-failover-live.json)
- [Worker runtime failure/recovery test](worker-runtime-tests.log)
- [Initial recovery routing](promoted-route.json)
- [Full refresh and resume requests](refresh-request.json), [resume](refresh-resume-request.json)
- [Run outcomes](refresh-run-outcomes.json) and [build/publication/cleanup milestones](refresh-milestones.json)
- [Controller tests](startup-controller-tests.log)
- [Final admission and conditional routing](refresh-promoted-route.json)
- [Final public/scoped checks and reader metadata](refresh-public-verified.json)
- [Final public smoke](final-public-smoke.log) and [ranking acceptance](final-ranking.log)
- [Publisher pins](publisher-config.json), [live schedule/concurrency](final-deployment.json)
- [Final resource inventory and cleanup](final-resources.json)

The public smoke pass excludes authenticated admin mutations and database-plan
checks; those were not part of this outage repair. The benchmark's legacy
backstop suggestion is not a deployment action: automatic fallback remains on.
Worker version is `6e57ccd1-782e-4674-8f3e-e6c884c89fac`; the manual emergency flag
was removed only after automatic fallback was deployed and tested against the
unavailable primary.

Bluesky fallback is degraded unscoped service. PDS-scoped requests fail explicitly
when the primary is unavailable. Both-provider failure still returns 503, and the
Worker cannot rescue an outage of its own hosting platform. Cloud capacity and
future refresh success are not guaranteed by one verified cycle; the independent
fallback and documented preflight protect recovery from depending on that claim.
